from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation

from app.instruments.option_chain import OptionChainService
from app.scanner.explanation import ScannerExplanationService
from app.scanner.repository import ScannerRepository
from app.scanner.scoring import OpportunityScoringService
from app.targets.calculation import TargetCalculationService


class ScannerService:
    def __init__(self, session, provider, redis, *, stale_after_seconds: int = 15):
        self.session = session
        self.provider = provider
        self.redis = redis
        self.stale_after_seconds = stale_after_seconds
        self.repository = ScannerRepository(session)

    async def opportunities(self, *, underlying: str, expiry: date, filters: dict, offset: int = 0) -> dict:
        symbol = underlying.strip().upper()
        universe = await self.repository.scan_contracts(symbol, expiry)
        if not universe or not universe["contracts"]:
            return self._empty(symbol, expiry, "No active Dhan-confirmed contracts are synchronized for this underlying and expiry.",
                               quality_status="DATA UNAVAILABLE")
        mapping = {(Decimal(row["strike"]), str(row["option_type"]).upper()): row
                   for row in universe["contracts"]}
        if self.provider is None:
            return self._empty(symbol, expiry, "Dhan option-chain provider is unavailable.", quality_status="DATA UNAVAILABLE")
        chain = await OptionChainService(self.provider, _RepositoryAdapter(self.session), self.redis,
                                         stale_after_seconds=self.stale_after_seconds).get_chain(symbol, expiry)
        if not chain.get("available"):
            return self._empty(symbol, expiry, chain.get("message", "Dhan option-chain data is unavailable."),
                                quality_status=chain.get("status", "DATA UNAVAILABLE"))
        age = self._age(chain.get("source_timestamp"))
        stale = bool(chain.get("stale")) or chain.get("status") == "DATA STALE" or age is None or age > filters["max_data_age_seconds"]
        spot = self._positive(chain.get("underlying_spot"))
        directional = None
        if not stale and spot is not None:
            directional = await self.repository.underlying_momentum(
                universe["underlying"]["underlying_instrument_id"],
                max_age_seconds=600)

        candidates = []
        history_keys = set()
        for strike_row in chain.get("strikes", []):
            try:
                parsed_strike = Decimal(str(strike_row["strike"]))
            except (KeyError, InvalidOperation, TypeError, ValueError):
                continue
            for side, option_type in (("call", "CALL"), ("put", "PUT")):
                contract = mapping.get((parsed_strike, option_type))
                if contract is not None and strike_row.get(side) is not None:
                    history_keys.add((parsed_strike, option_type))
        historical_cache = await self.repository.historical_10x_many(
            underlying_id=universe["underlying"]["underlying_id"],
            contracts=sorted(history_keys, key=lambda item: (item[0], item[1])))
        for strike_row in chain.get("strikes", []):
            try:
                strike = Decimal(str(strike_row["strike"]))
            except (KeyError, InvalidOperation, TypeError, ValueError):
                continue
            for side, option_type in (("call", "CALL"), ("put", "PUT")):
                quote = strike_row.get(side)
                if quote is None:
                    continue
                contract_row = mapping.get((strike, option_type))
                candidates.append(await self._candidate(
                    symbol=symbol, expiry=expiry, strike=strike, option_type=option_type,
                    quote=quote, contract_row=contract_row, chain=chain, chain_age=age,
                    stale=stale, spot=spot, directional=directional, filters=filters,
                    historical_cache=historical_cache,
                ))

        included = [item for item in candidates if item["included"]]
        candidate_count = len(candidates)
        included_count = len(included)
        included.sort(key=lambda item: (item["total_score"] or 0), reverse=True)
        candidates.sort(key=lambda item: (not item["included"], -(item["total_score"] or 0)))
        limit = filters["limit"]
        truncated = offset + limit < len(candidates)
        candidates = candidates[offset:offset + limit]
        included_returned = [item for item in candidates if item["included"]]
        return {
            "status": "OPPORTUNITIES FOUND" if included_returned else "NO CLEAR OPPORTUNITY",
            "source": "DHANHQ", "underlying": symbol, "expiry": expiry.isoformat(),
            "scan_timestamp": datetime.now(timezone.utc),
            "data_timestamp": chain.get("source_timestamp"),
            "timestamp_kind": chain.get("timestamp_kind", "backend_received_at"),
            "data_quality_status": "DATA STALE" if stale else "RECENT RESPONSE; CONTRACT EVENT TIME UNAVAILABLE",
            "source_event_timestamp_available": chain.get("provider_timestamp") is not None,
            "spot": spot, "spot_momentum": directional,
            "score_model": {"weights": OpportunityScoringService.weights(),
                            "minimum_score": filters["minimum_score"],
                            "minimum_available_weight_pct": filters["minimum_score_coverage_pct"],
                            "missing_component_policy": "renormalize total over available component weights; no missing component is assigned zero"},
            "filters": {k: (str(v) if isinstance(v, Decimal) else v) for k, v in filters.items()},
            "counts": {"contracts_considered": candidate_count,
                       "opportunities_before_limit": included_count,
                       "opportunities_returned": len(included_returned),
                       "excluded_before_limit": max(0, candidate_count - included_count),
                       "truncated": truncated, "returned_candidates": len(candidates),
                       "offset": offset, "limit": limit,
                       "next_offset": offset + limit if truncated else None},
            "opportunities": included_returned,
            "candidates": candidates,
            "message": "No qualifying candidate is available from fresh provider data and validated history." if not included_returned else "Candidates are ranked observations, not predictions or guarantees.",
            "trade_execution": False,
        }

    async def _candidate(self, *, symbol, expiry, strike, option_type, quote, contract_row,
                         chain, chain_age, stale, spot, directional, filters, historical_cache):
        reasons, exclusions = [], []
        premium = self._positive(quote.get("ltp"))
        volume = self._nonnegative_int(quote.get("volume"))
        oi = self._nonnegative_int(quote.get("open_interest"))
        previous_oi = self._nonnegative_int(quote.get("previous_oi"))
        change_oi = (oi - previous_oi) if oi is not None and previous_oi is not None else quote.get("change_in_oi")
        change_oi_pct = (change_oi / previous_oi * 100.0) if change_oi is not None and previous_oi and previous_oi > 0 else None
        iv = self._nonnegative_number(quote.get("implied_volatility"))
        bid_value, ask_value = self._nonnegative_number(quote.get("bid")), self._nonnegative_number(quote.get("ask"))
        bid = bid_value if bid_value and bid_value > 0 else None
        ask = ask_value if ask_value and ask_value > 0 else None
        bid_qty, ask_qty = self._nonnegative_int(quote.get("bid_quantity")), self._nonnegative_int(quote.get("ask_quantity"))
        previous_close_value = self._nonnegative_number(quote.get("previous_close"))
        previous_close = previous_close_value if previous_close_value and previous_close_value > 0 else None
        premium_change = premium - previous_close if premium is not None and previous_close is not None else None
        premium_change_pct = premium_change / previous_close * 100.0 if premium_change is not None and previous_close else None
        spread = ask - bid if ask is not None and bid is not None else None
        spread_pct = spread / premium * 100.0 if spread is not None and premium else None
        liquidity_qty = bid_qty + ask_qty if bid_qty is not None and ask_qty is not None else None
        distance_pct = abs(float(strike) - spot) / spot * 100.0 if spot and spot > 0 else None

        if stale:
            exclusions.append("Dhan option-chain response is stale or older than the configured freshness limit.")
        if premium is None:
            exclusions.append("Current premium is missing, non-positive, or invalid.")
        for field, raw, parsed in (
            ("volume", quote.get("volume"), volume), ("open interest", quote.get("open_interest"), oi),
            ("previous OI", quote.get("previous_oi"), previous_oi),
            ("IV", quote.get("implied_volatility"), iv), ("bid", quote.get("bid"), bid),
            ("ask", quote.get("ask"), ask), ("bid quantity", quote.get("bid_quantity"), bid_qty),
            ("ask quantity", quote.get("ask_quantity"), ask_qty),
            ("previous close", quote.get("previous_close"), previous_close_value),
        ):
            if raw is not None and parsed is None:
                exclusions.append(f"Dhan {field} field is malformed or outside its valid range.")
        if ((bid is None and bid_qty not in (None, 0)) or
                (ask is None and ask_qty not in (None, 0))):
            exclusions.append("Dhan displayed quote size has no corresponding positive bid or ask price.")
        if volume is None or oi is None:
            # Missing values remain unavailable; min filters below decide whether to exclude.
            reasons.append("Volume or OI is unavailable; its scoring component is omitted if unavailable.")
        if contract_row is None or str(contract_row.get("security_id")) != str(quote.get("security_id")):
            exclusions.append("Dhan chain contract does not match the synchronized instrument master.")
        if bid is not None and ask is not None and ask < bid:
            exclusions.append("Bid/ask is crossed and fails quote validation.")

        if filters["minimum_premium"] is not None and (premium is None or premium < filters["minimum_premium"]):
            exclusions.append("Premium is below the requested minimum or unavailable.")
        if filters["maximum_premium"] is not None and (premium is None or premium > filters["maximum_premium"]):
            exclusions.append("Premium exceeds the requested maximum or is unavailable.")
        if filters["minimum_volume"] is not None and (volume is None or volume < filters["minimum_volume"]):
            exclusions.append("Volume is below the requested minimum or unavailable.")
        if filters["minimum_oi"] is not None and (oi is None or oi < filters["minimum_oi"]):
            exclusions.append("OI is below the requested minimum or unavailable.")
        if filters["minimum_change_in_oi"] is not None and (change_oi is None or change_oi < filters["minimum_change_in_oi"]):
            exclusions.append("Change in OI is below the requested minimum or unavailable.")
        if filters["minimum_iv"] is not None and (iv is None or iv < filters["minimum_iv"]):
            exclusions.append("IV is below the requested minimum or unavailable.")
        if filters["maximum_iv"] is not None and (iv is None or iv > filters["maximum_iv"]):
            exclusions.append("IV exceeds the requested maximum or is unavailable.")
        if filters["maximum_distance_pct"] is not None and (distance_pct is None or distance_pct > filters["maximum_distance_pct"]):
            exclusions.append("Distance from spot exceeds the requested maximum or spot is unavailable.")
        if filters["maximum_spread_pct"] is not None and (spread_pct is None or spread_pct > filters["maximum_spread_pct"]):
            exclusions.append("Bid/ask spread exceeds the requested maximum or is unavailable.")
        if filters["minimum_liquidity_quantity"] is not None and (liquidity_qty is None or liquidity_qty < filters["minimum_liquidity_quantity"]):
            exclusions.append("Displayed top-of-book liquidity is below the requested minimum or unavailable.")

        direction_return = directional.get("change_pct") if directional else None
        components = {
            "direction_momentum": OpportunityScoringService.direction_momentum(direction_return, option_type),
            "premium_momentum": OpportunityScoringService.premium_momentum(premium_change_pct),
            "volume": OpportunityScoringService.volume(volume),
            "iv": OpportunityScoringService.iv(iv),
            "distance_from_spot": OpportunityScoringService.distance(distance_pct),
        }
        oi_component, oi_subcomponents = OpportunityScoringService.oi_and_change(oi, change_oi_pct)
        components["oi_and_change_in_oi"] = oi_component
        historical = historical_cache.get((strike, option_type)) or {
            "available": False, "source": "DHANHQ expired-options rolling history",
            "reason": "No validated observations were found for the same underlying, actual strike and option type."}
        history_multiple = float(historical["maximum_multiple"]) if historical.get("available") else None
        components["historical_10x"] = OpportunityScoringService.historical_10x(history_multiple)
        total = OpportunityScoringService.total(components)
        if total["score"] is None:
            exclusions.append("No scoring component has usable source evidence.")
        elif total["available_weight_pct"] < filters["minimum_score_coverage_pct"]:
            exclusions.append("Available scoring evidence is below the minimum model coverage threshold.")
        elif total["score"] < filters["minimum_score"]:
            exclusions.append("Total score is below the configured minimum opportunity threshold.")
        if premium is not None and not exclusions:
            reasons.extend(["Fresh Dhan option-chain response passed the requested market filters.",
                            "Contract identity is validated against the synchronized Dhan instrument master."])
        included = not exclusions
        targets = ([{"multiplier": level.multiplier, "target_value": level.target_value}
                    for level in TargetCalculationService.calculate(Decimal(str(premium)), option_type)]
                   if premium is not None and premium > 0 else None)
        return {
            "included": included, "underlying": symbol, "expiry": expiry.isoformat(), "strike": strike,
            "option_type": option_type, "current_premium": premium, "targets": targets,
            "volume": volume, "open_interest": oi, "change_in_oi": change_oi,
            "change_in_oi_pct": change_oi_pct, "implied_volatility": iv,
            "bid": bid, "ask": ask, "spread": spread, "spread_pct": spread_pct,
            "top_bid_quantity": bid_qty, "top_ask_quantity": ask_qty,
            "displayed_liquidity_quantity": liquidity_qty,
            "underlying_spot": spot, "distance_from_spot_pct": distance_pct,
            "premium_change": premium_change, "premium_change_pct": premium_change_pct,
            "historical_statistics": historical,
            "component_scores": components,
            "component_details": {"oi_and_change_in_oi": oi_subcomponents,
                                  "underlying_momentum": directional,
                                  "normalization_weights": OpportunityScoringService.weights()},
            "total_score": round(total["score"], 4) if total["score"] is not None else None,
            "score_coverage": {"available_weight_pct": total["available_weight_pct"],
                               "missing_components": total["missing_components"]},
            "data_timestamp": chain.get("source_timestamp"),
            "provider_event_timestamp": chain.get("provider_timestamp"),
            "data_age_seconds": chain_age, "chain_cache_hit": chain.get("cache_hit"),
            "data_quality_status": "DATA STALE" if stale else "RECENT RESPONSE; OPTION EVENT TIME UNAVAILABLE",
            "stale": stale, "source": "DHANHQ", "security_id": quote.get("security_id"),
            "instrument_id": str(contract_row["instrument_id"]) if contract_row else None,
            "option_contract_id": str(contract_row["option_contract_id"]) if contract_row else None,
            "explanation": ScannerExplanationService.explain(components, total, included, reasons, exclusions),
        }

    @staticmethod
    def _positive(value):
        try:
            number = float(value)
            return number if number > 0 and number < float("inf") else None
        except (TypeError, ValueError, OverflowError):
            return None

    @staticmethod
    def _nonnegative_number(value):
        try:
            number = float(value)
            return number if number >= 0 and number < float("inf") else None
        except (TypeError, ValueError, OverflowError):
            return None

    @staticmethod
    def _nonnegative_int(value):
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            return None
        return value

    @staticmethod
    def _age(value):
        if not isinstance(value, str):
            return None
        try:
            timestamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if timestamp.tzinfo is None:
                return None
            return max(0.0, (datetime.now(timezone.utc) - timestamp.astimezone(timezone.utc)).total_seconds())
        except ValueError:
            return None

    @staticmethod
    def _empty(underlying, expiry, message, quality_status="DATA UNAVAILABLE"):
        status = "DATA UNAVAILABLE" if quality_status == "DATA UNAVAILABLE" else "NO CLEAR OPPORTUNITY"
        return {"status": status, "source": "DHANHQ", "underlying": underlying,
                "expiry": expiry.isoformat(), "opportunities": [], "candidates": [],
                "counts": {"contracts_considered": 0, "opportunities_before_limit": 0,
                           "opportunities_returned": 0, "excluded_before_limit": 0,
                           "truncated": False, "returned_candidates": 0},
                "data_quality_status": quality_status, "message": message,
                "trade_execution": False}


class _RepositoryAdapter:
    """OptionChainService needs only these repository methods for contract validation."""

    def __init__(self, session):
        from app.instruments.repository import InstrumentRepository
        self._repository = InstrumentRepository(session)

    async def resolve_underlying(self, symbol):
        return await self._repository.resolve_underlying(symbol)

    async def has_option_expiry(self, underlying_id, expiry):
        return await self._repository.has_option_expiry(underlying_id, expiry)

from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, InvalidOperation

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.instruments.option_chain import OptionChainService
from app.instruments.repository import InstrumentRepository


PHASES = ("PAST", "PRESENT", "NEXT", "FUTURE")
COMPARISONS = (("PAST", "PRESENT"), ("PRESENT", "NEXT"), ("NEXT", "FUTURE"),
               ("PAST", "NEXT"), ("PAST", "FUTURE"))


class PastPresentNextFutureService:
    """Compare exact Dhan observations; never forecast or fill absent values."""

    def __init__(self, session: AsyncSession, provider, redis, *, stale_after_seconds: int = 15):
        self.session = session
        self.provider = provider
        self.redis = redis
        self.stale_after_seconds = stale_after_seconds

    async def analyze(self, *, underlying: str, strike: Decimal, option_type: str,
                      past_expiry: date, present_expiry: date, next_expiry: date,
                      future_expiry: date, past_from: date, past_to: date) -> dict:
        symbol = underlying.strip().upper()
        kind = option_type.strip().upper()
        if not symbol or len(symbol) > 40:
            raise ValueError("underlying must be a non-empty Dhan symbol up to 40 characters")
        if kind not in {"CALL", "PUT"}:
            raise ValueError("option_type must be CALL or PUT")
        try:
            strike = strike if isinstance(strike, Decimal) else Decimal(strike)
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise ValueError("strike must be a valid positive Dhan strike") from exc
        if not strike.is_finite() or strike <= 0 or strike > Decimal("1000000000") or strike.as_tuple().exponent < -8:
            raise ValueError("strike must be a valid positive Dhan strike with at most 8 decimals")
        expiries = (past_expiry, present_expiry, next_expiry, future_expiry)
        if not expiries[0] < expiries[1] < expiries[2] < expiries[3]:
            raise ValueError("Select strictly ascending, distinct expiries for PAST, PRESENT, NEXT and FUTURE")
        if past_from > past_to or (past_to - past_from).days > 3660:
            raise ValueError("past_from/past_to must be an ordered historical window of at most 10 years")
        today_utc = datetime.now(timezone.utc).date()
        if past_expiry >= today_utc:
            raise ValueError("PAST expiry must be historical; select a separate expired contract")

        past = await self._past_observation(symbol, strike, kind, past_expiry, past_from, past_to)
        selected = {"PAST": past}
        repository = InstrumentRepository(self.session)
        chain_service = OptionChainService(self.provider, repository, self.redis,
                                           stale_after_seconds=self.stale_after_seconds)
        for phase, expiry in zip(PHASES[1:], expiries[1:]):
            try:
                chain = await chain_service.get_chain(symbol, expiry)
            except Exception as exc:
                # Provider errors become an explicit unavailable phase, never a generated value.
                selected[phase] = self._unavailable(phase, expiry, "Dhan observation request failed",
                                                    source="DHANHQ", error_type=type(exc).__name__)
                continue
            selected[phase] = self._from_chain(chain, phase, expiry, strike, kind)

        complete = sum(1 for phase in PHASES if selected[phase].get("available"))
        fresh = sum(1 for phase in PHASES if selected[phase].get("available") and not selected[phase].get("stale", False))
        stale = sum(1 for phase in PHASES if selected[phase].get("available") and selected[phase].get("stale", False))
        comparisons = [self._compare(source, destination, selected[source], selected[destination],
                                     symbol, strike, kind)
                       for source, destination in COMPARISONS]
        historical_max = await self._historical_maximum(symbol, strike, kind, past_expiry, past_from, past_to)
        return {
            "status": "AVAILABLE" if complete == len(PHASES) else "PARTIAL DATA",
            "source": "DHANHQ", "underlying": symbol, "strike": strike, "option_type": kind,
            "selected_expiries": {phase: expiry.isoformat() for phase, expiry in zip(PHASES, expiries)},
            "past_window": {"from": past_from.isoformat(), "to": past_to.isoformat(), "to_inclusive": True},
            "phases": selected, "comparisons": comparisons, "historical_maximum_multiple": historical_max,
            "data_completeness": {"available_phases": complete, "total_phases": len(PHASES),
                                  "fraction": Decimal(complete) / Decimal(len(PHASES)),
                                  "missing_phases": [p for p in PHASES if not selected[p].get("available")],
                                  "fresh_phases": fresh, "stale_phases": stale},
            "quality": {"real_provider_observations_only": True, "interpolation": False,
                        "synthetic_future_prices": False,
                        "expiry_note": "Different expiries are different contracts. NEXT and FUTURE are actual provider quotes for their selected expiries at the source timestamps shown, not future price predictions."},
        }

    async def _past_observation(self, symbol: str, strike: Decimal, kind: str,
                                expiry: date, start: date, end: date) -> dict:
        result = await self.session.execute(text("""
          with subject as (
            select u.id underlying_id,e.id expiry_id,c.id contract_id,c.instrument_id
            from public.underlying_assets u
            join public.option_expiries e on e.underlying_id=u.id and e.expiry_date=:expiry
            join public.option_contracts c on c.underlying_id=u.id and c.option_expiry_id=e.id
              and c.strike=:strike and c.option_type=:option_type
            join public.instruments i on i.id=c.instrument_id
            join public.market_data_providers p on p.id=i.provider_id and p.code='DHANHQ'
            where upper(u.root_symbol)=:symbol and i.exchange_segment='NSE_FNO'
          ), observations as (
            select x.provider_timestamp,x.ingested_at,coalesce(x.close_price,x.last_price) premium,
                   'DHANHQ expired-options history'::text source,
                   jsonb_build_object('table','expired_option_observations','observation_kind',x.observation_kind,
                     'expiry_flag',x.expiry_flag,'expiry_code',x.expiry_code,'interval',x.interval) metadata
            from public.expired_option_observations x join subject s
              on s.underlying_id=x.underlying_id and s.expiry_id=x.option_expiry_id
            join public.market_data_providers p on p.id=x.provider_id and p.code='DHANHQ'
            where x.strike=:strike and x.option_type=:option_type and x.source='DHANHQ'
              and x.provider_timestamp >= :from_ts and x.provider_timestamp < :to_ts
              and coalesce(x.close_price,x.last_price) > 0
            union all
            select h.provider_timestamp,h.ingested_at,h.close_price,'DHANHQ historical candles'::text,
                   jsonb_build_object('table','historical_candles','timeframe',h.timeframe)
            from public.historical_candles h join subject s on s.instrument_id=h.instrument_id
            join public.market_data_providers p on p.id=h.provider_id and p.code='DHANHQ'
            where h.candle_timestamp >= :from_ts and h.candle_timestamp < :to_ts and h.close_price > 0
          )
          select * from observations order by provider_timestamp desc,ingested_at desc limit 1
        """), {"symbol": symbol, "expiry": expiry, "strike": strike,
               "option_type": kind.lower(), "from_ts": datetime.combine(start, time.min, timezone.utc),
               "to_ts": datetime.combine(end + timedelta(days=1), time.min, timezone.utc)})
        row = result.mappings().one_or_none()
        if row is None:
            return self._unavailable("PAST", expiry,
                "No positive Dhan history exists for this exact underlying, expiry, strike, option type and date window.",
                source="DHANHQ")
        return {"phase": "PAST", "available": True, "status": "HISTORICAL", "source": row["source"],
                "premium": Decimal(row["premium"]), "provider_timestamp": row["provider_timestamp"],
                "ingested_at": row["ingested_at"], "expiry": expiry.isoformat(), "strike": strike,
                "option_type": kind, "metadata": row["metadata"], "stale": False,
                "timestamp_kind": "provider_timestamp"}

    async def _historical_maximum(self, symbol: str, strike: Decimal, kind: str, expiry: date,
                                 start: date, end: date) -> dict:
        result = await self.session.execute(text("""
          with subject as (
            select u.id underlying_id,e.id expiry_id,c.instrument_id
            from public.underlying_assets u join public.option_expiries e
              on e.underlying_id=u.id and e.expiry_date=:expiry
            join public.option_contracts c on c.underlying_id=u.id and c.option_expiry_id=e.id
              and c.strike=:strike and c.option_type=:option_type
            where upper(u.root_symbol)=:symbol
          ), observations as (
            select x.provider_timestamp,coalesce(x.close_price,x.last_price) premium
            from public.expired_option_observations x join subject s
              on s.underlying_id=x.underlying_id and s.expiry_id=x.option_expiry_id
            join public.market_data_providers p on p.id=x.provider_id and p.code='DHANHQ'
            where x.source='DHANHQ' and x.strike=:strike and x.option_type=:option_type
              and x.provider_timestamp >= :from_ts and x.provider_timestamp < :to_ts
              and coalesce(x.close_price,x.last_price)>0
            union all
            select h.provider_timestamp,h.close_price from public.historical_candles h join subject s
              on s.instrument_id=h.instrument_id
            join public.market_data_providers p on p.id=h.provider_id and p.code='DHANHQ'
            where h.candle_timestamp >= :from_ts and h.candle_timestamp < :to_ts and h.close_price>0
          ) select provider_timestamp,premium from observations order by premium desc,provider_timestamp asc limit 1
        """), {"symbol": symbol, "expiry": expiry, "strike": strike, "option_type": kind.lower(),
               "from_ts": datetime.combine(start, time.min, timezone.utc),
               "to_ts": datetime.combine(end + timedelta(days=1), time.min, timezone.utc)})
        peak = result.mappings().one_or_none()
        baseline = await self.session.execute(text("""
          with subject as (
            select u.id underlying_id,e.id expiry_id,c.instrument_id
            from public.underlying_assets u join public.option_expiries e
              on e.underlying_id=u.id and e.expiry_date=:expiry
            join public.option_contracts c on c.underlying_id=u.id and c.option_expiry_id=e.id
              and c.strike=:strike and c.option_type=:option_type where upper(u.root_symbol)=:symbol
          ), observations as (
            select x.provider_timestamp,coalesce(x.close_price,x.last_price) premium
            from public.expired_option_observations x join subject s
              on s.underlying_id=x.underlying_id and s.expiry_id=x.option_expiry_id
            join public.market_data_providers p on p.id=x.provider_id and p.code='DHANHQ'
            where x.source='DHANHQ' and x.strike=:strike and x.option_type=:option_type
              and x.provider_timestamp >= :from_ts and x.provider_timestamp < :to_ts
              and coalesce(x.close_price,x.last_price)>0
            union all
            select h.provider_timestamp,h.close_price from public.historical_candles h join subject s
              on s.instrument_id=h.instrument_id
            join public.market_data_providers p on p.id=h.provider_id and p.code='DHANHQ'
            where h.candle_timestamp >= :from_ts and h.candle_timestamp < :to_ts and h.close_price>0
          ) select provider_timestamp,premium from observations order by provider_timestamp asc limit 1
        """), {"symbol": symbol, "expiry": expiry, "strike": strike, "option_type": kind.lower(),
               "from_ts": datetime.combine(start, time.min, timezone.utc),
               "to_ts": datetime.combine(end + timedelta(days=1), time.min, timezone.utc)})
        first = baseline.mappings().one_or_none()
        if peak is None or first is None:
            return {"available": False, "reason": "No positive observations exist for the selected historical contract and window.",
                    "expiry": expiry.isoformat(), "source": "DHANHQ"}
        peak_price, first_price = Decimal(peak["premium"]), Decimal(first["premium"])
        return {"available": True, "multiple_from_first_observation": peak_price / first_price,
                "baseline_premium": first_price, "baseline_timestamp": first["provider_timestamp"],
                "maximum_premium": peak_price, "maximum_timestamp": peak["provider_timestamp"],
                "expiry": expiry.isoformat(), "strike": strike, "option_type": kind,
                "source": "DHANHQ", "definition": "maximum observed premium divided by earliest observed premium in the selected window"}

    @staticmethod
    def _from_chain(chain: dict, phase: str, expiry: date, strike: Decimal, kind: str) -> dict:
        if not chain.get("available"):
            return PastPresentNextFutureService._unavailable(phase, expiry,
                chain.get("message") or chain.get("status") or "Dhan option-chain observation unavailable",
                source=chain.get("source", "DHANHQ"))
        row = next((item for item in chain.get("strikes", [])
                    if Decimal(str(item.get("strike"))) == strike), None)
        contract = row.get("call" if kind == "CALL" else "put") if row else None
        if not isinstance(contract, dict):
            return PastPresentNextFutureService._unavailable(phase, expiry,
                "Dhan returned no contract for the selected strike and option type.", source="DHANHQ")
        if contract.get("ltp") is None or Decimal(str(contract["ltp"])) <= 0:
            return PastPresentNextFutureService._unavailable(phase, expiry,
                "Dhan returned no valid positive premium for this contract.", source="DHANHQ")
        timestamp = chain.get("source_timestamp")
        stale = bool(chain.get("stale")) or chain.get("status") == "DATA STALE"
        return {"phase": phase, "available": True, "status": "DATA STALE" if stale else "OBSERVED",
                "source": "DHANHQ option chain", "premium": Decimal(str(contract["ltp"])),
                "provider_timestamp": chain.get("provider_timestamp"), "source_timestamp": timestamp,
                "ingested_at": timestamp, "timestamp_kind": chain.get("timestamp_kind", "backend_received_at"),
                "expiry": expiry.isoformat(), "strike": strike, "option_type": kind,
                "security_id": contract.get("security_id"), "stale": stale,
                "quality": {"chain_status": chain.get("status"), "cache_hit": chain.get("cache_hit"),
                            "age_seconds": chain.get("age_seconds"),
                            "provider_event_timestamp_available": chain.get("provider_timestamp") is not None}}

    @staticmethod
    def _unavailable(phase: str, expiry: date, reason: str, **extra) -> dict:
        return {"phase": phase, "available": False, "status": "UNAVAILABLE", "reason": reason,
                "expiry": expiry.isoformat(), **extra}

    @staticmethod
    def _compare(source_phase: str, destination_phase: str, source: dict, destination: dict,
                 underlying: str, strike: Decimal, kind: str) -> dict:
        base = {"comparison": f"{source_phase} → {destination_phase}",
                "underlying": underlying, "strike": strike, "option_type": kind,
                "source_expiry": source.get("expiry"), "destination_expiry": destination.get("expiry"),
                "source_timestamp": source.get("provider_timestamp") or source.get("source_timestamp"),
                "destination_timestamp": destination.get("provider_timestamp") or destination.get("source_timestamp"),
                "different_contracts": source.get("expiry") != destination.get("expiry"),
                "same_underlying_strike_option_type": True}
        if not source.get("available") or not destination.get("available"):
            missing = [phase for phase, value in ((source_phase, source), (destination_phase, destination))
                       if not value.get("available")]
            return {**base, "available": False, "reason": "One or both required real observations are unavailable.",
                    "missing_phases": missing}
        a, b = Decimal(source["premium"]), Decimal(destination["premium"])
        if a <= 0 or b <= 0:
            return {**base, "available": False, "reason": "Comparison requires positive observed premiums."}
        a_time = source.get("provider_timestamp") or source.get("source_timestamp")
        b_time = destination.get("provider_timestamp") or destination.get("source_timestamp")
        elapsed = None
        if a_time and b_time:
            if isinstance(a_time, str):
                a_time = datetime.fromisoformat(a_time.replace("Z", "+00:00"))
            if isinstance(b_time, str):
                b_time = datetime.fromisoformat(b_time.replace("Z", "+00:00"))
            elapsed = b_time - a_time
        return {**base, "available": True, "source_premium": a, "destination_premium": b,
                "premium_change": b-a, "percentage_change": (b-a)/a*Decimal(100),
                "premium_multiple": b/a, "time_between_observations": elapsed,
                "source_data_quality": source.get("quality", {"status": source.get("status"), "source": source.get("source")}),
                "destination_data_quality": destination.get("quality", {"status": destination.get("status"), "source": destination.get("source")}),
                "historical_vs_current": source_phase == "PAST" or destination_phase == "PAST",
                "interpretation": "Observed premiums from separate expiry contracts; this is a cross-expiry comparison." if base["different_contracts"] else "Observed premiums from the same expiry contract."}

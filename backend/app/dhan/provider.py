import asyncio
import hashlib
import json
import math
from datetime import date, datetime, timezone
from typing import Any, AsyncIterator
from urllib.parse import urlencode

import structlog
import websockets
from pydantic import ValidationError

from app.dhan.client import DhanRestClient
from app.dhan.config import DhanConfig
from app.dhan.errors import DhanInvalidResponse, DhanRequestError, DhanUnavailableError
from app.dhan.interfaces import MarketDataProvider
from app.dhan.schemas import (
    EXCHANGE_SEGMENTS,
    EXPIRED_FIELDS,
    INSTRUMENT_TYPES,
    INTERVALS,
    CandleResponse,
    QuoteRequest,
    UnderlyingRequest,
    validate_csv_master,
)

log = structlog.get_logger(__name__)
FEED_URL = "wss://api-feed.dhan.co"


class DhanProvider(MarketDataProvider):
    name = "DHANHQ"
    CACHE_TTLS = {
        "quote": 1,
        "option_chain": 3,
        "expiry": 300,
        "historical": 3600,
        "expired_options": 3600,
        "instrument_master": 3600,
    }

    def __init__(self, config: DhanConfig, rest_client: DhanRestClient, redis_client=None, connection_monitor=None):
        self.config = config
        self.rest = rest_client
        self.redis = redis_client
        self.connection_monitor = connection_monitor

    async def get_market_quotes(self, instruments: dict[str, list[int]], kind: str = "ltp") -> dict[str, Any]:
        request = QuoteRequest(instruments=instruments)
        paths = {"ltp": "marketfeed/ltp", "ohlc": "marketfeed/ohlc", "quote": "marketfeed/quote"}
        if kind not in paths:
            raise DhanRequestError("Quote kind must be ltp, ohlc, or quote.")
        body = request.instruments
        validator = lambda data: self._validate_quote_data(data, kind)
        return await self._cached(f"quote_{kind}", paths[kind], body, lambda: self.rest.request_json("POST", paths[kind], bucket="quote", json_body=body, include_client_id=True), self.CACHE_TTLS["quote"], self._quote_timestamp, validator)

    @staticmethod
    def _validate_quote_data(data: Any, kind: str) -> None:
        if not isinstance(data, dict):
            raise DhanInvalidResponse("Dhan market-quote response data must be an object.")
        for segment_data in data.values():
            if not isinstance(segment_data, dict):
                raise DhanInvalidResponse("Dhan market-quote segment data is malformed.")
            for quote in segment_data.values():
                if not isinstance(quote, dict):
                    raise DhanInvalidResponse("Dhan market-quote instrument data is malformed.")
                if not isinstance(quote.get("last_price"), (int, float)) or not math.isfinite(quote["last_price"]):
                    raise DhanInvalidResponse("Dhan market quote contains an invalid last price.")
                ohlc = quote.get("ohlc")
                if kind in {"ohlc", "quote"} and (not isinstance(ohlc, dict) or any(not isinstance(ohlc.get(field), (int, float)) or not math.isfinite(ohlc[field]) for field in ("open", "high", "low", "close"))):
                    raise DhanInvalidResponse("Dhan market quote contains malformed OHLC data.")

    @staticmethod
    def _validate_option_chain_data(data: Any) -> None:
        if not isinstance(data, dict):
            raise DhanInvalidResponse("Dhan option-chain response has no documented data object.")
        if not isinstance(data.get("last_price"), (int, float)) or isinstance(data.get("last_price"), bool) or not math.isfinite(data["last_price"]) or not isinstance(data.get("oc"), dict):
            raise DhanInvalidResponse("Dhan option-chain response does not match its documented schema.")
        for strike, sides in data["oc"].items():
            try:
                float(strike)
            except (TypeError, ValueError) as exc:
                raise DhanInvalidResponse("Dhan option-chain contains an invalid strike.") from exc
            if not isinstance(sides, dict):
                raise DhanInvalidResponse("Dhan option-chain strike data is malformed.")
            for side in ("ce", "pe"):
                contract = sides.get(side)
                if contract is None:
                    continue
                if not isinstance(contract, dict):
                    raise DhanInvalidResponse("Dhan option-chain contract data is malformed.")
                if "security_id" not in contract or not isinstance(contract["security_id"], int) or isinstance(contract["security_id"], bool) or contract["security_id"] <= 0:
                    raise DhanInvalidResponse("Dhan option-chain contract has no valid security ID.")
                for field in ("average_price", "last_price", "oi", "previous_close_price", "previous_oi", "previous_volume", "volume", "top_bid_price", "top_ask_price", "top_bid_quantity", "top_ask_quantity", "implied_volatility"):
                    if field in contract and contract[field] is not None and (not isinstance(contract[field], (int, float)) or isinstance(contract[field], bool) or not math.isfinite(contract[field])):
                        raise DhanInvalidResponse("Dhan option-chain contract contains an invalid numeric field.")
                greeks = contract.get("greeks")
                if greeks is not None and (not isinstance(greeks, dict) or any(value is not None and (not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value)) for value in greeks.values())):
                    raise DhanInvalidResponse("Dhan option-chain Greeks are malformed.")

    async def get_option_chain(self, underlying_scrip: int, underlying_segment: str, expiry: date) -> dict[str, Any]:
        underlying = UnderlyingRequest(underlying_scrip=underlying_scrip, underlying_segment=underlying_segment)
        body = {"UnderlyingScrip": underlying.underlying_scrip, "UnderlyingSeg": underlying.underlying_segment, "Expiry": expiry.isoformat()}
        return await self._cached("option_chain", "optionchain", body, lambda: self.rest.request_json("POST", "optionchain", bucket="option_chain", json_body=body, include_client_id=True), self.CACHE_TTLS["option_chain"], validator=self._validate_option_chain_data)

    async def get_expiries(self, underlying_scrip: int, underlying_segment: str) -> dict[str, Any]:
        underlying = UnderlyingRequest(underlying_scrip=underlying_scrip, underlying_segment=underlying_segment)
        body = {"UnderlyingScrip": underlying.underlying_scrip, "UnderlyingSeg": underlying.underlying_segment}
        def validate(values: Any) -> None:
            if not isinstance(values, list):
                raise DhanInvalidResponse("Dhan expiry-list response does not match its documented schema.")
            try:
                for value in values:
                    date.fromisoformat(value)
            except (TypeError, ValueError) as exc:
                raise DhanInvalidResponse("Dhan expiry-list contains an invalid date.") from exc

        return await self._cached("expiry", "optionchain/expirylist", body, lambda: self.rest.request_json("POST", "optionchain/expirylist", bucket="option_chain", json_body=body, include_client_id=True), self.CACHE_TTLS["expiry"], validator=validate)

    async def get_historical_candles(
        self,
        security_id: str,
        exchange_segment: str,
        instrument: str,
        from_date: date | datetime,
        to_date: date | datetime,
        interval: str | None = None,
        oi: bool = False,
        expiry_code: int | None = None,
    ) -> dict[str, Any]:
        self._validate_scrip(security_id, exchange_segment, instrument)
        self._validate_date_range(from_date, to_date)
        body: dict[str, Any] = {
            "securityId": security_id,
            "exchangeSegment": exchange_segment,
            "instrument": instrument,
            "oi": oi,
            "fromDate": self._format_datetime(from_date, timed=interval is not None),
            "toDate": self._format_datetime(to_date, timed=interval is not None),
        }
        if expiry_code is not None:
            if expiry_code not in {0, 1, 2}:
                raise DhanRequestError("expiry_code must use Dhan's documented 0, 1, or 2 values.")
            body["expiryCode"] = expiry_code
        if interval is None:
            path = "charts/historical"
        else:
            if str(interval) not in INTERVALS:
                raise DhanRequestError("interval must be one of Dhan's documented minute intervals.")
            if self._duration_seconds(from_date, to_date) > 90 * 86400:
                raise DhanRequestError("Dhan intraday history permits at most 90 days per request.")
            path = "charts/intraday"
            body["interval"] = str(interval)
        return await self._cached("historical", path, body, lambda: self.rest.request_json("POST", path, bucket="data", json_body=body), self.CACHE_TTLS["historical"], self._series_timestamp, self._validate_candles)

    @staticmethod
    def _validate_candles(data: Any) -> None:
        try:
            CandleResponse.model_validate(data)
        except (ValidationError, TypeError) as exc:
            raise DhanInvalidResponse("Dhan candle response does not match the documented OHLCV schema.") from exc

    async def get_expired_options(
        self,
        *,
        exchange_segment: str,
        security_id: str,
        instrument: str,
        interval: str,
        expiry_flag: str,
        expiry_code: int,
        strike: str,
        option_type: str,
        required_data: list[str],
        from_date: date,
        to_date: date,
    ) -> dict[str, Any]:
        self._validate_scrip(security_id, exchange_segment, instrument)
        self._validate_date_range(from_date, to_date)
        if str(interval) not in INTERVALS or expiry_flag not in {"WEEK", "MONTH"} or expiry_code not in {0, 1, 2}:
            raise DhanRequestError("Expired-options interval or expiry parameters are invalid per Dhan v2 documentation.")
        if option_type not in {"CALL", "PUT"}:
            raise DhanRequestError("option_type must be CALL or PUT.")
        if not required_data or not set(required_data).issubset(EXPIRED_FIELDS):
            raise DhanRequestError("required_data contains unsupported Dhan expired-options fields.")
        if (to_date - from_date).days > 30:
            raise DhanRequestError("Dhan expired-options history permits at most 30 days per request.")
        body = {
            "exchangeSegment": exchange_segment,
            "interval": str(interval),
            "securityId": int(security_id),
            "instrument": instrument,
            "expiryFlag": expiry_flag,
            "expiryCode": expiry_code,
            "strike": strike,
            "drvOptionType": option_type,
            "requiredData": required_data,
            "fromDate": from_date.isoformat(),
            "toDate": to_date.isoformat(),
        }
        return await self._cached("expired_options", "charts/rollingoption", body, lambda: self.rest.request_json("POST", "charts/rollingoption", bucket="data", json_body=body), self.CACHE_TTLS["expired_options"], self._expired_timestamp, self._validate_expired_data)

    @staticmethod
    def _validate_expired_data(data: Any) -> None:
        if not isinstance(data, dict) or not all(side in data for side in ("ce", "pe")):
            raise DhanInvalidResponse("Dhan expired-options response does not match its documented schema.")
        for side in ("ce", "pe"):
            values = data[side]
            if values is None:
                continue
            if not isinstance(values, dict) or not isinstance(values.get("timestamp"), list):
                raise DhanInvalidResponse("Dhan expired-options series is malformed.")
            for name, series in values.items():
                if isinstance(series, list) and name != "timestamp" and len(series) != len(values["timestamp"]):
                    raise DhanInvalidResponse("Dhan expired-options series are misaligned.")

    async def get_instrument_master(self, detailed: bool = False) -> dict[str, Any]:
        body = {"detailed": bool(detailed)}
        fetch = self._fetch_master_detailed if detailed else self._fetch_master
        validator = lambda data: self._validate_master_data(data, detailed)
        return await self._cached("instrument_master", "official-instrument-master-detailed.csv" if detailed else "official-instrument-master.csv", body, fetch, self.CACHE_TTLS["instrument_master"], validator=validator)

    @staticmethod
    def _validate_master_data(data: Any, detailed: bool = False) -> None:
        if not isinstance(data, dict) or data.get("format") != "csv" or not isinstance(data.get("content"), str):
            raise DhanInvalidResponse("Dhan instrument master response is malformed.")
        try:
            validate_csv_master(data["content"], detailed=detailed)
        except ValueError as exc:
            raise DhanInvalidResponse("Dhan instrument master CSV is malformed.") from exc

    async def _fetch_master(self) -> dict[str, Any]:
        text, _ = await self.rest.download_instrument_master_csv()
        try:
            header, row_count = validate_csv_master(text)
        except ValueError as exc:
            raise DhanInvalidResponse("Dhan instrument master CSV is malformed.") from exc
        return {"format": "csv", "content": text, "columns": header, "row_count": row_count}

    async def _fetch_master_detailed(self) -> dict[str, Any]:
        text, _ = await self.rest.download_instrument_master_csv(detailed=True)
        try:
            header, row_count = validate_csv_master(text, detailed=True)
        except ValueError as exc:
            raise DhanInvalidResponse("Dhan instrument master CSV is malformed.") from exc
        return {"format": "csv", "content": text, "columns": header, "row_count": row_count}

    async def stream_market_data(self, instruments: list[tuple[str, str]], mode: str = "ticker") -> AsyncIterator[dict[str, Any]]:
        if not self.config.is_configured:
            raise DhanUnavailableError("Dhan credentials are not configured")
        if mode != "ticker":
            raise DhanRequestError("This provider currently subscribes to Dhan's documented ticker feed only.")
        if len(instruments) > 5000 or (not instruments and self.redis is None):
            raise DhanRequestError("Dhan feed requires up to 5000 instruments and Redis for dynamic subscriptions when the initial list is empty.")
        normalized = []
        for segment, security_id in instruments:
            if segment not in EXCHANGE_SEGMENTS or not str(security_id).isdigit() or int(security_id) <= 0:
                raise DhanRequestError("Feed instruments must use official Dhan segments and IDs from the instrument master.")
            pair = (segment, str(security_id))
            if pair not in normalized:
                normalized.append(pair)
        query = urlencode({"version": "2", "token": self.config.access_token, "clientId": self.config.client_id, "authType": "2"})
        url = f"{FEED_URL}?{query}"
        subscription_task = None
        try:
            async with websockets.connect(url, open_timeout=self.config.timeout_seconds, close_timeout=5, ping_interval=None) as socket:
                if self.connection_monitor:
                    self.connection_monitor.record_feed(True)
                for start in range(0, len(normalized), 100):
                    batch = normalized[start:start + 100]
                    await socket.send(json.dumps({
                        "RequestCode": 15,
                        "InstrumentCount": len(batch),
                        "InstrumentList": [{"ExchangeSegment": segment, "SecurityId": security_id} for segment, security_id in batch],
                    }))
                if self.redis is not None:
                    subscription_task = asyncio.create_task(
                        self._listen_for_dynamic_subscriptions(socket, set(normalized)),
                        name="dhan-dynamic-subscriptions")
                async for message in socket:
                    if not isinstance(message, bytes):
                        continue
                    if message and message[0] == 50:
                        if self.connection_monitor:
                            self.connection_monitor.record_feed(False, "provider_disconnect")
                        raise DhanUnavailableError("Dhan disconnected the live feed.")
                    # Keep packet parsing in the established, independently documented decoder.
                    from app.market_data import decode_ticker_packet

                    quote = decode_ticker_packet(message)
                    if quote is None:
                        continue
                    ingested_at = datetime.now(timezone.utc).isoformat()
                    quote.update({"source": self.name, "ingested_at": ingested_at, "received_at": ingested_at})
                    if self.connection_monitor:
                        self.connection_monitor.record_feed_packet()
                    yield quote
        except asyncio.CancelledError:
            raise
        except DhanUnavailableError:
            raise
        except Exception as exc:
            if self.connection_monitor:
                self.connection_monitor.record_feed(False, type(exc).__name__)
            raise DhanUnavailableError("Dhan live feed disconnected or failed.") from exc
        finally:
            if subscription_task is not None:
                subscription_task.cancel()
                try:
                    await subscription_task
                except asyncio.CancelledError:
                    pass
            if self.connection_monitor:
                self.connection_monitor.record_feed(False, "connection_closed")

    async def _listen_for_dynamic_subscriptions(self, socket, known: set[tuple[str, str]]) -> None:
        pubsub = self.redis.pubsub()
        try:
            await pubsub.subscribe("market:subscriptions:add")
            async for message in pubsub.listen():
                if message.get("type") != "message":
                    continue
                try:
                    payload = json.loads(message["data"])
                    segment = payload.get("exchange_segment")
                    security_id = str(payload.get("security_id", ""))
                    if (payload.get("source") != self.name or segment not in EXCHANGE_SEGMENTS
                            or not security_id.isdigit() or int(security_id) <= 0):
                        continue
                    pair = (segment, security_id)
                    # Redis publishers may request only IDs that were resolved
                    # from synchronized Dhan mappings and persisted user items.
                    if not await self.redis.sismember("market:subscriptions:validated", f"{segment}:{security_id}"):
                        continue
                    if pair in known or len(known) >= 5000:
                        continue
                    await socket.send(json.dumps({
                        "RequestCode": 15, "InstrumentCount": 1,
                        "InstrumentList": [{"ExchangeSegment": segment, "SecurityId": security_id}],
                    }))
                    known.add(pair)
                except (json.JSONDecodeError, TypeError, ValueError, KeyError):
                    log.warning("dhan_dynamic_subscription_invalid")
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    log.warning("dhan_dynamic_subscription_failed", error_type=type(exc).__name__)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("dhan_dynamic_subscription_listener_failed", error_type=type(exc).__name__)
        finally:
            try:
                await pubsub.unsubscribe("market:subscriptions:add")
                await pubsub.aclose()
            except Exception:
                pass

    async def _cached(self, name: str, endpoint: str, request: dict[str, Any], fetch, ttl: int, timestamp_extractor=None, validator=None) -> dict[str, Any]:
        key = self._cache_key(name, request)
        if self.config.cache_enabled and self.redis is not None:
            try:
                raw = await self.redis.get(key)
                if raw:
                    cached = json.loads(raw)
                    if validator:
                        validator(cached.get("data"))
                    cached["provenance"]["cache_hit"] = True
                    return cached
            except Exception as exc:
                log.warning("dhan_cache_read_failed", endpoint=name, error_type=type(exc).__name__)
                try:
                    await self.redis.delete(key)
                except Exception:
                    pass
        result = await fetch()
        if isinstance(result, dict) and "data" in result and "status" in result:
            data = result["data"]
            if str(result.get("status", "")).lower() != "success":
                raise DhanInvalidResponse("Dhan returned a response without a successful provider status.")
        else:
            data = result
        if validator:
            validator(data)
        ingested_at = datetime.now(timezone.utc).isoformat()
        provider_timestamp = timestamp_extractor(data) if timestamp_extractor else None
        envelope = {
            "data": data,
            "provenance": {
                "provider": self.name,
                "endpoint": endpoint,
                "provider_timestamp": provider_timestamp,
                "ingested_at": ingested_at,
                "cache_hit": False,
            },
        }
        if self.config.cache_enabled and self.redis is not None:
            try:
                await self.redis.set(key, json.dumps(envelope, separators=(",", ":")), ex=ttl)
            except Exception as exc:
                log.warning("dhan_cache_write_failed", endpoint=name, error_type=type(exc).__name__)
        return envelope

    def _cache_key(self, name: str, request: dict[str, Any]) -> str:
        canonical = json.dumps(request, sort_keys=True, separators=(",", ":"))
        digest = hashlib.sha256(canonical.encode()).hexdigest()
        client_hash = hashlib.sha256(self.config.client_id.encode()).hexdigest()[:16]
        return f"dhan:provider:{client_hash}:{name}:{digest}"

    @staticmethod
    def _quote_timestamp(data: Any) -> str | None:
        if not isinstance(data, dict):
            return None
        for segment in data.values():
            if isinstance(segment, dict):
                for quote in segment.values():
                    if isinstance(quote, dict) and isinstance(quote.get("last_trade_time"), str):
                        return quote["last_trade_time"]
        return None

    @staticmethod
    def _series_timestamp(data: Any) -> int | None:
        values = data.get("timestamp") if isinstance(data, dict) else None
        return max(values) if isinstance(values, list) and values and all(isinstance(item, int) for item in values) else None

    @staticmethod
    def _expired_timestamp(data: Any) -> int | None:
        if not isinstance(data, dict):
            return None
        all_timestamps = []
        for side in ("ce", "pe"):
            series = data.get(side)
            timestamps = series.get("timestamp") if isinstance(series, dict) else None
            if isinstance(timestamps, list):
                all_timestamps.extend(ts for ts in timestamps if isinstance(ts, int))
        return max(all_timestamps) if all_timestamps else None

    @staticmethod
    def _validate_scrip(security_id: str, segment: str, instrument: str) -> None:
        if segment not in EXCHANGE_SEGMENTS or instrument not in INSTRUMENT_TYPES or not str(security_id).isdigit() or int(security_id) <= 0:
            raise DhanRequestError("Use an official Dhan segment, instrument type, and security ID from the Dhan instrument master.")

    @staticmethod
    def _format_datetime(value: date | datetime, timed: bool) -> str:
        if timed:
            moment = value if isinstance(value, datetime) else datetime.combine(value, datetime.min.time())
            return moment.strftime("%Y-%m-%d %H:%M:%S")
        return value.date().isoformat() if isinstance(value, datetime) else value.isoformat()

    @staticmethod
    def _duration_seconds(start: date | datetime, end: date | datetime) -> float:
        left = datetime.combine(start, datetime.min.time(), tzinfo=timezone.utc) if isinstance(start, date) and not isinstance(start, datetime) else start
        right = datetime.combine(end, datetime.min.time(), tzinfo=timezone.utc) if isinstance(end, date) and not isinstance(end, datetime) else end
        if left.tzinfo is not None:
            left = left.astimezone(timezone.utc).replace(tzinfo=None)
        if right.tzinfo is not None:
            right = right.astimezone(timezone.utc).replace(tzinfo=None)
        if left >= right:
            raise DhanRequestError("from_date must be earlier than to_date.")
        return (right - left).total_seconds()

    @classmethod
    def _validate_date_range(cls, start: date | datetime, end: date | datetime) -> None:
        cls._duration_seconds(start, end)

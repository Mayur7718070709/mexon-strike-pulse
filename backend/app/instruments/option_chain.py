import asyncio
import hashlib
import json
import time
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from uuid import uuid4

import structlog

from app.dhan.errors import DhanInvalidResponse, DhanUnavailableError
from app.instruments.repository import InstrumentRepository

log = structlog.get_logger(__name__)


class OptionChainService:
    """Resolves only synced NSE mappings and coalesces shared Dhan chain requests."""

    CACHE_SECONDS = 3
    STALE_SECONDS = 6 * 60 * 60
    LOCK_MILLISECONDS = 30_000
    _local_locks: dict[str, asyncio.Lock] = {}
    _local_cache: dict[str, tuple[float, dict]] = {}
    _local_stale: dict[str, dict] = {}

    def __init__(self, provider, repository: InstrumentRepository, redis_client, stale_after_seconds: int = 15):
        self.provider = provider
        self.repository = repository
        self.redis = redis_client
        self.stale_after_seconds = stale_after_seconds

    async def get_chain(self, underlying: str, expiry: date) -> dict:
        normalized = underlying.strip().upper()
        resolved = await self.repository.resolve_underlying(normalized)
        if resolved is None:
            return self._unavailable(normalized, expiry, "No active Dhan underlying mapping is synchronized.")
        if not await self.repository.has_option_expiry(resolved["underlying_id"], expiry):
            return self._unavailable(normalized, expiry, "Dhan has not confirmed this expiry in the synchronized instrument master.")

        cache_key = "market:option-chain:" + hashlib.sha256(f"{normalized}:{expiry.isoformat()}".encode()).hexdigest()
        local_lock = self._local_locks.setdefault(cache_key, asyncio.Lock())
        async with local_lock:
            cached = await self._read_cache(cache_key)
            if cached:
                return self._with_freshness(cached, cache_hit=True)

            lock_token = None
            lock_key = f"{cache_key}:lock"
            if self.redis is not None:
                lock_token = uuid4().hex
                try:
                    acquired = await self.redis.set(lock_key, lock_token, nx=True, px=self.LOCK_MILLISECONDS)
                except Exception as exc:
                    raise DhanUnavailableError("Shared option-chain coordination is unavailable.") from exc
                if not acquired:
                    cached = await self._wait_for_shared_result(cache_key, lock_key)
                    if cached:
                        return self._with_freshness(cached, cache_hit=True)
                    # Re-attempt the lock after a leader exits without a fresh response.
                    lock_token = uuid4().hex
                    try:
                        acquired = await self.redis.set(lock_key, lock_token, nx=True, px=self.LOCK_MILLISECONDS)
                    except Exception as exc:
                        raise DhanUnavailableError("Shared option-chain coordination is unavailable.") from exc
                    if not acquired:
                        return await self._stale_or_raise(cache_key, DhanUnavailableError("A shared Dhan option-chain request is still in progress."))

            try:
                # Recheck after winning the distributed lock in case another worker populated the cache.
                cached = await self._read_cache(cache_key)
                if cached:
                    return self._with_freshness(cached, cache_hit=True)
                try:
                    envelope = await self.provider.get_option_chain(int(resolved["security_id"]), resolved["exchange_segment"], expiry)
                    normalized_result = self._normalize(envelope, normalized, expiry)
                    try:
                        await self._write_cache(cache_key, normalized_result)
                    except DhanUnavailableError as exc:
                        log.warning("option_chain_cache_write_failed", error_type=type(exc).__name__)
                    return self._with_freshness(normalized_result, cache_hit=False)
                except Exception as exc:
                    log.warning("dhan_option_chain_request_failed", underlying=normalized,
                                expiry=expiry.isoformat(), error_type=type(exc).__name__)
                    return await self._stale_or_raise(cache_key, exc)
            finally:
                if lock_token and self.redis is not None:
                    try:
                        await self.redis.eval(
                            "if redis.call('get', KEYS[1]) == ARGV[1] then return redis.call('del', KEYS[1]) else return 0 end",
                            1, lock_key, lock_token,
                        )
                    except Exception:
                        pass

    async def _read_cache(self, key: str) -> dict | None:
        if self.redis is not None:
            try:
                raw = await self.redis.get(key)
                if raw:
                    return json.loads(raw)
            except Exception as exc:
                raise DhanUnavailableError("Shared option-chain cache is unavailable.") from exc
        cached = self._local_cache.get(key)
        if cached and cached[0] > time.monotonic():
            return cached[1]
        return None

    async def _read_stale(self, key: str) -> dict | None:
        if self.redis is not None:
            try:
                raw = await self.redis.get(f"{key}:stale")
                return json.loads(raw) if raw else None
            except Exception:
                return None
        return self._local_stale.get(key)

    async def _write_cache(self, key: str, value: dict) -> None:
        if self.redis is not None:
            try:
                serialized = json.dumps(value, separators=(",", ":"))
                await self.redis.set(key, serialized, ex=self.CACHE_SECONDS)
                await self.redis.set(f"{key}:stale", serialized, ex=self.STALE_SECONDS)
            except Exception as exc:
                raise DhanUnavailableError("Shared option-chain cache could not be updated.") from exc
        else:
            self._local_cache[key] = (time.monotonic() + self.CACHE_SECONDS, value)
            self._local_stale[key] = value

    async def _wait_for_shared_result(self, key: str, lock_key: str) -> dict | None:
        deadline = time.monotonic() + self.LOCK_MILLISECONDS / 1000
        while time.monotonic() < deadline:
            cached = await self._read_cache(key)
            if cached:
                return cached
            try:
                if not await self.redis.exists(lock_key):
                    return None
            except Exception as exc:
                raise DhanUnavailableError("Shared option-chain coordination is unavailable.") from exc
            await asyncio.sleep(0.1)
        return None

    async def _stale_or_raise(self, key: str, error: Exception) -> dict:
        stale = await self._read_stale(key)
        if stale is None:
            raise error
        stale = {**stale, "stale": True, "status": "DATA STALE",
                 "provider_error": "Dhan did not return a current option-chain response."}
        try:
            await self._write_cache(key, stale)
        except DhanUnavailableError:
            pass
        return self._with_freshness(stale, cache_hit=True, force_stale=True)

    def _with_freshness(self, value: dict, *, cache_hit: bool, force_stale: bool = False) -> dict:
        fetched_at = datetime.fromisoformat(value["source_timestamp"])
        age = max(0.0, (datetime.now(timezone.utc) - fetched_at).total_seconds())
        stale = force_stale or value.get("stale", False) or age > self.stale_after_seconds
        # Dhan's documented chain body has no source event timestamp, so a new
        # response is marked RECENT rather than claiming an exchange-time LIVE tick.
        return {**value, "status": "DATA STALE" if stale else "RECENT RESPONSE", "stale": stale,
                "cache_hit": cache_hit, "age_seconds": int(age)}

    @staticmethod
    def _unavailable(underlying: str, expiry: date, message: str) -> dict:
        return {"status": "DATA UNAVAILABLE", "available": False, "source": "DHANHQ",
                "underlying": underlying, "expiry": expiry.isoformat(), "strikes": [], "message": message}

    @classmethod
    def _normalize(cls, envelope: dict, underlying: str, expiry: date) -> dict:
        data = envelope.get("data")
        if not isinstance(data, dict) or not isinstance(data.get("oc"), dict):
            raise DhanInvalidResponse("Dhan option-chain response is not a strike map.")
        received_at = (envelope.get("provenance") or {}).get("ingested_at")
        if not received_at:
            raise DhanInvalidResponse("Dhan response is missing its backend receive timestamp.")
        spot = cls._number(data.get("last_price"))
        if spot is None:
            raise DhanInvalidResponse("Dhan option-chain response is missing underlying spot price.")
        strikes = []
        for strike_text, sides in data["oc"].items():
            try:
                strike = float(Decimal(str(strike_text)))
            except (InvalidOperation, ValueError) as exc:
                raise DhanInvalidResponse("Dhan option-chain contains an invalid strike.") from exc
            if not isinstance(sides, dict):
                raise DhanInvalidResponse("Dhan option-chain strike row is malformed.")
            strikes.append({"strike": strike, "call": cls._contract(sides.get("ce")), "put": cls._contract(sides.get("pe"))})
        strikes.sort(key=lambda row: row["strike"])
        provenance = envelope.get("provenance") or {}
        return {
            "available": True, "source": "DHANHQ", "underlying": underlying, "expiry": expiry.isoformat(),
            "underlying_spot": spot, "source_timestamp": received_at,
            # Current Dhan option-chain v2 response does not include an exchange event timestamp.
            "provider_timestamp": provenance.get("provider_timestamp"),
            "timestamp_kind": "backend_received_at", "strikes": strikes,
        }

    @classmethod
    def _contract(cls, item: dict | None) -> dict | None:
        if item is None:
            return None
        if not isinstance(item, dict):
            raise DhanInvalidResponse("Dhan option-chain contract is malformed.")
        security_id = item.get("security_id")
        if not isinstance(security_id, (int, float)) or isinstance(security_id, bool) or int(security_id) <= 0:
            raise DhanInvalidResponse("Dhan option-chain contract has no valid security ID.")
        ltp = cls._number(item.get("last_price"))
        previous_close = cls._number(item.get("previous_close_price"))
        oi = cls._integer(item.get("oi"))
        previous_oi = cls._integer(item.get("previous_oi"))
        greeks = item.get("greeks") if isinstance(item.get("greeks"), dict) else {}
        premium_change = ltp - previous_close if ltp is not None and previous_close is not None else None
        percentage_change = (premium_change / previous_close * 100) if premium_change is not None and previous_close != 0 else None
        return {
            "security_id": str(int(security_id)), "ltp": ltp, "premium_change": premium_change,
            "percentage_change": percentage_change,
            "previous_close": previous_close, "previous_oi": previous_oi,
            "bid": cls._number(item.get("top_bid_price")), "ask": cls._number(item.get("top_ask_price")),
            "bid_quantity": cls._integer(item.get("top_bid_quantity")), "ask_quantity": cls._integer(item.get("top_ask_quantity")),
            "volume": cls._integer(item.get("volume")), "open_interest": oi,
            "change_in_oi": oi - previous_oi if oi is not None and previous_oi is not None else None,
            "implied_volatility": cls._number(item.get("implied_volatility")),
            "greeks": {key: cls._number(greeks.get(key)) for key in ("delta", "theta", "gamma", "vega")},
        }

    @staticmethod
    def _number(value):
        if value is None or isinstance(value, bool):
            return None
        if not isinstance(value, (int, float)):
            raise DhanInvalidResponse("Dhan option-chain numeric field is malformed.")
        if not float("-inf") < float(value) < float("inf"):
            raise DhanInvalidResponse("Dhan option-chain numeric field is not finite.")
        return float(value)

    @staticmethod
    def _integer(value):
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, int):
            raise DhanInvalidResponse("Dhan option-chain quantity or open interest is malformed.")
        return value

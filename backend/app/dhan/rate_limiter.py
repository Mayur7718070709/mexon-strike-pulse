import asyncio
import hashlib
import random
import time
from collections import defaultdict
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo


class DhanRateLimiterUnavailable(RuntimeError):
    pass


class DhanDailyQuotaExceeded(RuntimeError):
    pass


class DhanRateLimiter:
    """Shared Redis-backed minimum-spacing limiter with a process-local dev mode."""

    INTERVALS = {"quote": 1.0, "data": 0.2, "option_chain": 3.0, "non_trading": 0.05}
    _shared_locks = defaultdict(asyncio.Lock)
    _shared_last_local: dict[str, float] = {}
    DATA_DAILY_LIMIT = 100_000

    def __init__(self, client_id: str, redis_client=None):
        scope = hashlib.sha256(client_id.encode()).hexdigest()[:20]
        self._prefix = f"dhan:rate:{scope}"
        self._redis = redis_client

    async def acquire(self, bucket: str) -> None:
        interval = self.INTERVALS.get(bucket)
        if interval is None:
            raise ValueError("Unknown Dhan rate-limit bucket")
        if self._redis is not None:
            key = f"{self._prefix}:{bucket}"
            ttl_ms = max(1, int(interval * 1000))
            deadline = time.monotonic() + 60.0
            while True:
                try:
                    acquired = await self._redis.set(key, "1", nx=True, px=ttl_ms)
                except Exception as exc:
                    raise DhanRateLimiterUnavailable("Shared Dhan rate limiter is unavailable") from exc
                if acquired:
                    if bucket == "data":
                        await self._acquire_data_daily_quota()
                    return
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise DhanRateLimiterUnavailable("Dhan rate-limit queue exceeded its wait bound")
                await asyncio.sleep(min(remaining, 0.05) + random.uniform(0.001, 0.01))
        local_key = f"{self._prefix}:{bucket}"
        async with self._shared_locks[local_key]:
            now = time.monotonic()
            delay = interval - (now - self._shared_last_local.get(local_key, 0.0))
            if delay > 0:
                await asyncio.sleep(delay)
            self._shared_last_local[local_key] = time.monotonic()

    async def _acquire_data_daily_quota(self) -> None:
        if self._redis is None:
            return
        now = datetime.now(ZoneInfo("Asia/Kolkata"))
        next_day = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        key = f"{self._prefix}:data:daily:{now.date().isoformat()}"
        try:
            ttl_seconds = max(60, int((next_day - now).total_seconds()) + 60)
            count = await self._redis.eval(
                "local n=redis.call('INCR',KEYS[1]); if n==1 then redis.call('EXPIRE',KEYS[1],ARGV[1]); end; return n",
                1, key, ttl_seconds,
            )
            if count > self.DATA_DAILY_LIMIT:
                await self._redis.decr(key)
                raise DhanDailyQuotaExceeded("Dhan's documented daily Data API request quota has been reached")
        except DhanDailyQuotaExceeded:
            raise
        except Exception as exc:
            raise DhanRateLimiterUnavailable("Shared Dhan daily quota counter is unavailable") from exc

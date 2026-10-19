import asyncio
from datetime import datetime, timezone

from app.dhan.config import DhanConfig


class DhanTokenManager:
    """Tracks expiry reported by Dhan; replacement is performed via server secret rotation."""

    def __init__(self, config: DhanConfig):
        self._config = config
        self._expiry: datetime | None = None
        self._lock = asyncio.Lock()

    @property
    def config(self) -> DhanConfig:
        return self._config

    @property
    def expires_at(self) -> datetime | None:
        return self._expiry

    async def record_expiry(self, expiry: datetime) -> None:
        async with self._lock:
            self._expiry = expiry.astimezone(timezone.utc)

    @property
    def is_expired(self) -> bool:
        return self._expiry is not None and self._expiry <= datetime.now(timezone.utc)

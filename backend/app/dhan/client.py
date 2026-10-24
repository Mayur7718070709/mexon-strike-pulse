import asyncio
import random
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any

import httpx
import structlog

from app.dhan.config import DhanConfig
from app.dhan.errors import (
    DhanAuthenticationError,
    DhanError,
    DhanErrorMapper,
    DhanInvalidResponse,
    DhanRateLimitError,
    DhanRequestError,
    DhanUnavailableError,
)
from app.dhan.rate_limiter import DhanDailyQuotaExceeded, DhanRateLimiter, DhanRateLimiterUnavailable

log = structlog.get_logger(__name__)

class DhanRestClient:
    """Authenticated, rate-limited DhanHQ v2 REST transport; no trading methods."""

    RETRIES = 2
    CSV_URLS = {
        False: "https://images.dhan.co/api-data/api-scrip-master.csv",
        True: "https://images.dhan.co/api-data/api-scrip-master-detailed.csv",
    }

    def __init__(self, config: DhanConfig, client: httpx.AsyncClient | None = None, redis_client=None):
        self.config = config
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(timeout=httpx.Timeout(config.timeout_seconds, connect=config.timeout_seconds))
        self.rate_limiter = DhanRateLimiter(config.client_id, redis_client)

    async def request_json(
        self,
        method: str,
        path: str,
        *,
        bucket: str,
        json_body: dict[str, Any] | None = None,
        include_client_id: bool = False,
    ) -> Any:
        self._ensure_configured()
        url = f"{self.config.base_url.rstrip('/')}/{path.lstrip('/')}"
        headers = {"access-token": self.config.access_token, "Accept": "application/json"}
        if json_body is not None:
            headers["Content-Type"] = "application/json"
        if include_client_id:
            headers["client-id"] = self.config.client_id

        for attempt in range(self.RETRIES + 1):
            try:
                await self.rate_limiter.acquire(bucket)
                response = await self._client.request(method, url, headers=headers, json=json_body)
            except DhanDailyQuotaExceeded as exc:
                raise DhanRateLimitError(str(exc)) from exc
            except DhanRateLimiterUnavailable as exc:
                raise DhanUnavailableError("Shared Dhan rate limiter is unavailable") from exc
            except httpx.TimeoutException as exc:
                if attempt < self.RETRIES:
                    await self._backoff(attempt)
                    continue
                raise DhanUnavailableError("Dhan request timed out") from exc
            except httpx.HTTPError as exc:
                if attempt < self.RETRIES:
                    await self._backoff(attempt)
                    continue
                raise DhanUnavailableError("Dhan network request failed") from exc

            payload = None
            try:
                payload = response.json()
            except ValueError:
                if response.is_success:
                    raise DhanInvalidResponse("Dhan returned a non-JSON response")
            provider_error = DhanErrorMapper.map_response(response.status_code, payload)
            if provider_error is not None:
                if (isinstance(provider_error, DhanRateLimitError) or response.status_code >= 500) and attempt < self.RETRIES:
                    retry_after = response.headers.get("Retry-After")
                    if self._retry_after_exceeds_bound(retry_after):
                        raise provider_error
                    await self._backoff(attempt, retry_after)
                    continue
                raise provider_error
            if not response.is_success:
                raise DhanUnavailableError("Dhan returned an unsuccessful response")
            if isinstance(payload, dict):
                body_error = DhanErrorMapper.map_response(200, payload)
                if body_error is not None:
                    if isinstance(body_error, DhanRateLimitError) and attempt < self.RETRIES:
                        retry_after = response.headers.get("Retry-After")
                        if self._retry_after_exceeds_bound(retry_after):
                            raise body_error
                        await self._backoff(attempt, retry_after)
                        continue
                    raise body_error
            return payload
        raise DhanUnavailableError("Dhan request attempts were exhausted")

    async def get_profile(self) -> dict[str, Any]:
        payload = await self.request_json("GET", "/profile", bucket="non_trading")
        if not isinstance(payload, dict):
            raise DhanInvalidResponse("Dhan profile response had an unexpected shape")
        return payload

    async def download_instrument_master_csv(self, detailed: bool = False) -> tuple[str, datetime]:
        """Download the official Dhan CSV master; it is a documented static file, not a REST JSON API."""
        url = self.CSV_URLS[detailed]
        for attempt in range(self.RETRIES + 1):
            try:
                await self.rate_limiter.acquire("data")
                response = await self._client.get(url, headers={"Accept": "text/csv"})
            except DhanRateLimiterUnavailable as exc:
                raise DhanUnavailableError("Shared Dhan rate limiter is unavailable") from exc
            except (httpx.TimeoutException, httpx.HTTPError) as exc:
                if attempt < self.RETRIES:
                    await self._backoff(attempt)
                    continue
                raise DhanUnavailableError("Dhan instrument master download failed") from exc
            if (response.status_code == 429 or response.status_code >= 500) and attempt < self.RETRIES:
                retry_after = response.headers.get("Retry-After")
                if self._retry_after_exceeds_bound(retry_after):
                    mapped = DhanErrorMapper.map_response(response.status_code)
                    raise mapped or DhanRateLimitError("Dhan requested a retry delay beyond the configured bound.")
                await self._backoff(attempt, retry_after)
                continue
            if not response.is_success:
                mapped = DhanErrorMapper.map_response(response.status_code)
                raise mapped or DhanUnavailableError("Dhan instrument master download failed")
            if len(response.content) > 100_000_000:
                raise DhanInvalidResponse("Dhan instrument master exceeded the allowed download size")
            return response.text, datetime.now(timezone.utc)
        raise DhanUnavailableError("Dhan instrument master attempts were exhausted")

    def _ensure_configured(self) -> None:
        if not self.config.is_configured:
            raise DhanUnavailableError("Dhan credentials are not configured")

    @staticmethod
    async def _backoff(attempt: int, retry_after: str | None = None) -> None:
        provider_delay = DhanRestClient._retry_after_seconds(retry_after)
        delay = min(60.0, max(provider_delay, 0.25 * (2**attempt)))
        await asyncio.sleep(delay + random.uniform(0.0, min(0.1, delay / 4)))

    @staticmethod
    def _retry_after_exceeds_bound(value: str | None) -> bool:
        return DhanRestClient._retry_after_seconds(value) > 60.0

    @staticmethod
    def _retry_after_seconds(value: str | None) -> float:
        if not value:
            return 0.0
        try:
            return max(0.0, float(value))
        except ValueError:
            try:
                retry_at = parsedate_to_datetime(value)
                if retry_at.tzinfo is None:
                    retry_at = retry_at.replace(tzinfo=timezone.utc)
                return max(0.0, (retry_at - datetime.now(timezone.utc)).total_seconds())
            except (TypeError, ValueError, OverflowError):
                return 0.0

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()


# Existing auth/status code imports DhanClient. Keep it as an alias, with no separate transport.
DhanClient = DhanRestClient

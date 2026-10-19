from typing import Any

import httpx

from app.dhan.config import DhanConfig


class DhanError(Exception):
    """Sanitized provider error; never includes headers, credentials, or body text."""


class DhanAuthenticationError(DhanError):
    pass


class DhanUnavailableError(DhanError):
    pass


class DhanClient:
    def __init__(self, config: DhanConfig, client: httpx.AsyncClient | None = None):
        self.config = config
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(timeout=httpx.Timeout(config.timeout_seconds))

    async def get_profile(self) -> dict[str, Any]:
        if not self.config.is_configured:
            raise DhanUnavailableError("Dhan credentials are not configured")
        try:
            response = await self._client.get(
                f"{self.config.base_url}/profile",
                headers={"access-token": self.config.access_token, "Accept": "application/json"},
            )
        except httpx.TimeoutException as exc:
            raise DhanUnavailableError("Dhan profile request timed out") from exc
        except httpx.HTTPError as exc:
            raise DhanUnavailableError("Dhan profile request failed") from exc

        if response.status_code in (401, 403):
            raise DhanAuthenticationError("Dhan rejected the access token")
        if response.status_code != 200:
            raise DhanUnavailableError("Dhan profile endpoint returned an unsuccessful response")
        try:
            payload = response.json()
        except ValueError as exc:
            raise DhanUnavailableError("Dhan profile response was not valid JSON") from exc
        if not isinstance(payload, dict):
            raise DhanUnavailableError("Dhan profile response had an unexpected shape")
        return payload

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

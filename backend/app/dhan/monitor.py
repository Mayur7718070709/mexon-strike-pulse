from datetime import datetime, timezone

import structlog

from app.dhan.auth import DhanAuthService
from app.dhan.client import DhanAuthenticationError, DhanUnavailableError
from app.dhan.token_manager import DhanTokenManager

log = structlog.get_logger(__name__)


class DhanConnectionMonitor:
    def __init__(self, auth: DhanAuthService, token_manager: DhanTokenManager):
        self.auth = auth
        self.token_manager = token_manager
        self.last_checked_at: datetime | None = None
        self.last_status: str = "NOT_CHECKED"

    async def status(self) -> dict:
        checked_at = datetime.now(timezone.utc)
        self.last_checked_at = checked_at
        if not self.token_manager.config.is_configured:
            return self._result("NOT_CONFIGURED", checked_at, "Dhan server credentials are not configured.")
        try:
            profile = await self.auth.validate_profile()
        except DhanAuthenticationError:
            return self._result("TOKEN_EXPIRED" if self.token_manager.is_expired else "AUTHENTICATION_FAILED", checked_at, "Dhan rejected the configured access token.")
        except DhanUnavailableError as exc:
            log.warning("dhan_profile_unavailable", error_type=type(exc).__name__)
            return self._result("DATA UNAVAILABLE", checked_at, "Dhan profile validation could not be completed.")
        except Exception as exc:
            log.error("dhan_profile_unexpected_error", error_type=type(exc).__name__)
            return self._result("DATA UNAVAILABLE", checked_at, "Dhan profile validation could not be completed.")
        self.last_status = "LIVE CONNECTED"
        return {"status": self.last_status, "connected": True, "checked_at": checked_at, "message": "Dhan profile validated successfully.", **profile}

    def _result(self, status: str, checked_at: datetime, message: str) -> dict:
        self.last_status = status
        return {"status": status, "connected": False, "checked_at": checked_at, "message": message, "token_expires_at": self.token_manager.expires_at, "active_segments": None, "ddpi_status": None, "mtf_status": None, "data_plan_status": None, "data_valid_until": None}

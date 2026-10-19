from datetime import datetime, timedelta, timezone

from app.dhan.client import DhanAuthenticationError, DhanClient, DhanUnavailableError
from app.dhan.token_manager import DhanTokenManager

IST = timezone(timedelta(hours=5, minutes=30), name="IST")


def _parse_dhan_datetime(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    value = value.strip()
    for fmt in ("%d/%m/%Y %H:%M", "%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(value, fmt).replace(tzinfo=IST).astimezone(timezone.utc)
        except ValueError:
            continue
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.replace(tzinfo=IST).astimezone(timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)
    except ValueError:
        return None


class DhanAuthService:
    def __init__(self, client: DhanClient, token_manager: DhanTokenManager):
        self.client = client
        self.token_manager = token_manager

    async def validate_profile(self) -> dict:
        profile = await self.client.get_profile()
        client_id = profile.get("dhanClientId")
        expiry = _parse_dhan_datetime(profile.get("tokenValidity"))
        if not isinstance(client_id, str) or client_id != self.token_manager.config.client_id or expiry is None:
            raise DhanUnavailableError("Dhan profile response did not satisfy the documented profile contract")
        if expiry <= datetime.now(timezone.utc):
            raise DhanAuthenticationError("Dhan access token is expired")
        await self.token_manager.record_expiry(expiry)
        return {
            "token_expires_at": expiry,
            "active_segments": profile.get("activeSegment") if isinstance(profile.get("activeSegment"), str) else None,
            "ddpi_status": profile.get("ddpi") if isinstance(profile.get("ddpi"), str) else None,
            "mtf_status": profile.get("mtf") if isinstance(profile.get("mtf"), str) else None,
            "data_plan_status": profile.get("dataPlan") if isinstance(profile.get("dataPlan"), str) else None,
            "data_valid_until": _parse_dhan_datetime(profile.get("dataValidity")),
        }

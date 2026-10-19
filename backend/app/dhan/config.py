from dataclasses import dataclass

from app.config import Settings


@dataclass(frozen=True)
class DhanConfig:
    client_id: str
    access_token: str
    base_url: str = "https://api.dhan.co/v2"
    timeout_seconds: float = 5.0

    @classmethod
    def from_settings(cls, settings: Settings) -> "DhanConfig":
        return cls(
            client_id=settings.dhan_client_id.strip(),
            access_token=settings.dhan_access_token.strip(),
            timeout_seconds=settings.dhan_http_timeout_seconds,
        )

    @property
    def is_configured(self) -> bool:
        return bool(self.client_id and self.access_token) and "YOUR_" not in self.client_id and "YOUR_" not in self.access_token

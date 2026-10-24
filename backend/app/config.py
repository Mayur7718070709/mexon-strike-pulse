from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_env: str = "development"
    log_level: str = "INFO"
    api_v1_prefix: str = "/api/v1"
    frontend_origin: str = "http://localhost:3000"
    database_url: str = ""
    redis_url: str = "redis://localhost:6379/0"
    dhan_client_id: str = ""
    dhan_access_token: str = ""
    dhan_http_timeout_seconds: float = 5.0
    dhan_cache_enabled: bool = False
    dhan_instruments: str = ""
    dhan_feed_mode: str = "ticker"
    market_data_stale_after_seconds: int = 15
    embedded_worker_enabled: bool = False
    supabase_url: str = ""
    supabase_anon_key: str = ""

    @property
    def configured_dhan_instruments(self) -> list[tuple[str, str]]:
        instruments: list[tuple[str, str]] = []
        for item in self.dhan_instruments.split(","):
            item = item.strip()
            if not item:
                continue
            segment, separator, security_id = item.partition(":")
            if not separator or not segment.strip() or not security_id.strip():
                raise ValueError("DHAN_INSTRUMENTS entries must use EXCHANGE_SEGMENT:SECURITY_ID")
            instruments.append((segment.strip(), security_id.strip()))
        if len(instruments) > 5000:
            raise ValueError("DhanHQ allows at most 5000 instruments per connection")
        return instruments

    @property
    def dhan_credentials_configured(self) -> bool:
        """Reject example values so they can never trigger an outbound Dhan request."""
        placeholders = {"YOUR_DHAN_CLIENT_ID", "YOUR_DHAN_ACCESS_TOKEN", ""}
        return self.dhan_client_id.strip() not in placeholders and self.dhan_access_token.strip() not in placeholders


@lru_cache
def get_settings() -> Settings:
    return Settings()

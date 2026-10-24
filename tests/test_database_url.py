from sqlalchemy.engine import make_url

from app.database import async_database_url


def test_standard_render_postgres_urls_use_asyncpg():
    for value in (
        "postgres://user:password@db.example.test:5432/marketpulse",
        "postgresql://user:password@db.example.test:5432/marketpulse?sslmode=require",
    ):
        normalized = make_url(async_database_url(value))
        assert normalized.drivername == "postgresql+asyncpg"
        assert normalized.password == "password"
        assert normalized.database == "marketpulse"


def test_existing_asyncpg_url_is_unchanged():
    value = "postgresql+asyncpg://user:password@db.example.test:5432/marketpulse"
    assert async_database_url(value) == value

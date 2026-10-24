from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.engine import make_url

from app.config import get_settings


def async_database_url(database_url: str) -> str:
    """Use the asyncpg dialect for standard PostgreSQL URLs in the async app."""
    url = make_url(database_url)
    if url.drivername in {"postgres", "postgresql", "postgresql+psycopg2", "postgresql+psycopg"}:
        return url.set(drivername="postgresql+asyncpg").render_as_string(hide_password=False)
    return database_url


settings = get_settings()
engine = create_async_engine(async_database_url(settings.database_url), pool_pre_ping=True) if settings.database_url else None
SessionLocal = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False) if engine else None


async def check_database() -> bool:
    if engine is None:
        return False
    try:
        async with engine.connect() as connection:
            await connection.exec_driver_sql("SELECT 1")
        return True
    except Exception:
        return False

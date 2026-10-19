"""Redis-backed worker entry point for durable background jobs."""
from arq.connections import RedisSettings

from app.config import get_settings


async def startup(ctx):
    ctx["settings"] = get_settings()


class WorkerSettings:
    # Register ingestion, target tracking, and alert jobs here with explicit retry policies.
    functions = []
    on_startup = startup
    max_jobs = 10
    job_timeout = 300
    max_tries = 5
    redis_settings = RedisSettings.from_dsn(get_settings().redis_url)

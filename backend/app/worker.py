"""Redis-backed worker entry point for durable background jobs."""
import asyncio
import time

from arq import cron
from arq.connections import RedisSettings
from sqlalchemy import text

from app.config import get_settings


async def startup(ctx):
    from app.dhan.client import DhanRestClient
    from app.dhan.config import DhanConfig
    from app.dhan.provider import DhanProvider
    from app.redis_client import redis

    settings = get_settings()
    config = DhanConfig.from_settings(settings)
    client = DhanRestClient(config, redis_client=redis)
    ctx["settings"] = settings
    ctx["historical_provider"] = DhanProvider(config, client, redis_client=redis)
    ctx["expired_options_provider"] = ctx["historical_provider"]
    ctx["dhan_client"] = client
    ctx["redis_client"] = redis
    from app.targets.monitoring_worker import run_live_target_monitor
    ctx["target_monitor_task"] = asyncio.create_task(
        run_live_target_monitor(redis), name="live-target-monitor")


async def shutdown(ctx):
    monitor_task = ctx.get("target_monitor_task")
    if monitor_task:
        monitor_task.cancel()
        try:
            await monitor_task
        except asyncio.CancelledError:
            pass
    client = ctx.get("dhan_client")
    if client:
        await client.close()
    redis = ctx.get("redis_client")
    if redis:
        await redis.aclose()
    from app.database import engine
    if engine is not None:
        await engine.dispose()


async def run_historical_ingestion(ctx, job_id: str):
    from app.database import SessionLocal
    from app.historical.worker import HistoricalIngestionWorker

    if SessionLocal is None:
        raise RuntimeError("Historical ingestion database is unavailable")
    return await HistoricalIngestionWorker(ctx["historical_provider"], SessionLocal).run(job_id)


async def run_expired_options_ingestion(ctx, job_id: str):
    from app.database import SessionLocal
    from app.expired_options.worker import ExpiredOptionsWorker

    if SessionLocal is None:
        raise RuntimeError("Expired-options ingestion database is unavailable")
    return await ExpiredOptionsWorker(ctx["expired_options_provider"], SessionLocal).run(job_id)


async def recover_stale_historical_jobs(ctx):
    """Requeue lost ARQ work; the DB worker claim prevents concurrent processing."""
    from app.database import SessionLocal

    if SessionLocal is None:
        return
    async with SessionLocal() as session:
        stale_running = await session.execute(text("""
          update public.historical_ingestion_jobs set status='queued',updated_at=now(),
            error_code='worker_recovery',error_message='Worker stopped before finishing; remaining chunks are being resumed'
          where status='running' and updated_at < now()-interval '15 minutes'
          returning id
        """))
        recovered_ids = [str(value) for value in stale_running.scalars()]
        queued = await session.execute(text("""
          select id from public.historical_ingestion_jobs
          where status='queued' and updated_at < now()-interval '5 minutes'
          order by created_at limit 100
        """))
        recovered_ids.extend(str(value) for value in queued.scalars())
        await session.commit()
    bucket = int(time.time() // 300)
    for job_id in dict.fromkeys(recovered_ids):
        await ctx["redis"].enqueue_job("run_historical_ingestion", job_id,
                                      _job_id=f"historical:{job_id}:recovery:{bucket}")


async def recover_stale_expired_options_jobs(ctx):
    """Requeue durable expired-option jobs interrupted by a worker restart."""
    from app.database import SessionLocal

    if SessionLocal is None:
        return
    async with SessionLocal() as session:
        stale = await session.execute(text("""
          update public.expired_options_ingestion_jobs set status='queued',updated_at=now(),
            error_code='worker_recovery',error_message='Worker stopped before finishing; remaining requests are resuming'
          where status='running' and updated_at < now()-interval '15 minutes' returning id
        """))
        ids = [str(value) for value in stale.scalars()]
        queued = await session.execute(text("""
          select id from public.expired_options_ingestion_jobs
          where status='queued' and updated_at < now()-interval '5 minutes'
          order by created_at limit 100
        """))
        ids.extend(str(value) for value in queued.scalars())
        await session.commit()
    bucket = int(time.time() // 300)
    for job_id in dict.fromkeys(ids):
        await ctx["redis"].enqueue_job("run_expired_options_ingestion", job_id,
                                      _job_id=f"expired-options:{job_id}:recovery:{bucket}")


class WorkerSettings:
    functions = [run_historical_ingestion, run_expired_options_ingestion]
    on_startup = startup
    on_shutdown = shutdown
    max_jobs = 10
    job_timeout = 1800
    max_tries = 5
    redis_settings = RedisSettings.from_dsn(get_settings().redis_url)
    cron_jobs = [cron(recover_stale_historical_jobs, minute=set(range(0, 60, 5)), run_at_startup=True),
                 cron(recover_stale_expired_options_jobs, minute=set(range(1, 60, 5)), run_at_startup=True)]

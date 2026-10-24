import asyncio
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api import router
from app.config import get_settings
from app.database import engine
from app.dhan.auth import DhanAuthService
from app.dhan.client import DhanRestClient
from app.dhan.config import DhanConfig
from app.dhan.health import ProviderHealthService
from app.dhan.monitor import DhanConnectionMonitor
from app.dhan.provider import DhanProvider
from app.dhan.token_manager import DhanTokenManager
from app.logging_config import configure_logging
from app.market_data import run_dhan_feed
from app.redis_client import redis

settings = get_settings()
configure_logging(settings.log_level)
log = structlog.get_logger(__name__)


async def monitor_target_staleness(redis_client, stale_after_seconds: int) -> None:
    """Persist unverified status when the Dhan feed stops producing fresh ticks."""
    from app.database import SessionLocal
    from app.targets.status import TargetStatusService

    if SessionLocal is None:
        log.warning("target_staleness_monitor_disabled", reason="database_unconfigured")
        return
    while True:
        try:
            async with SessionLocal() as session:
                await TargetStatusService(
                    session, redis_client, stale_after_seconds=stale_after_seconds
                ).mark_stale_sessions()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.error("target_staleness_monitor_failed", error_type=type(exc).__name__)
        await asyncio.sleep(5)


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.redis = redis
    dhan_config = DhanConfig.from_settings(settings)
    dhan_client = DhanRestClient(dhan_config, redis_client=redis)
    token_manager = DhanTokenManager(dhan_config)
    monitor = DhanConnectionMonitor(DhanAuthService(dhan_client, token_manager), token_manager)
    provider = DhanProvider(dhan_config, dhan_client, redis_client=redis, connection_monitor=monitor)
    app.state.dhan_monitor = monitor
    app.state.provider_health_service = ProviderHealthService(monitor)
    app.state.market_data_provider = provider
    feed_task = asyncio.create_task(run_dhan_feed(settings, redis, provider), name="dhan-live-feed")
    target_staleness_task = asyncio.create_task(
        monitor_target_staleness(redis, settings.market_data_stale_after_seconds),
        name="target-staleness-monitor",
    )
    yield
    for task in (feed_task, target_staleness_task):
        task.cancel()
    for task in (feed_task, target_staleness_task):
        try:
            await task
        except asyncio.CancelledError:
            pass
    await redis.aclose()
    await dhan_client.close()
    if engine is not None:
        await engine.dispose()


app = FastAPI(
    title="Market Pulse API",
    description="Versioned API for DhanHQ-backed market and options intelligence.",
    version="0.1.0",
    openapi_url=f"{settings.api_v1_prefix}/openapi.json",
    docs_url="/docs",
    redoc_url="/redoc",
    lifespan=lifespan,
)
app.add_middleware(CORSMiddleware, allow_origins=[settings.frontend_origin], allow_credentials=True, allow_methods=["GET", "POST"], allow_headers=["Authorization", "Content-Type"])
app.include_router(router, prefix=settings.api_v1_prefix)


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    log.exception("unhandled_api_error", path=request.url.path, error_type=type(exc).__name__)
    return JSONResponse(status_code=500, content={"detail": "Internal server error"})

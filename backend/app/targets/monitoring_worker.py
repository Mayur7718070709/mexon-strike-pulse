"""Durable target-monitoring consumer for raw Dhan feed observations."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import json
import os
import socket
import time

import structlog
from sqlalchemy import text

from app.alerts.engine import AlertEngine
from app.database import SessionLocal
from app.config import get_settings
from app.targets.status import TargetStatusService

log = structlog.get_logger(__name__)
STREAM = "market:target:observations"
GROUP = "market-pulse-target-monitor"
SUBSCRIPTIONS = "market:target:subscriptions"
VALIDATED_SUBSCRIPTIONS = "market:subscriptions:validated"


async def refresh_target_subscriptions(redis) -> None:
    """Keep only currently tracked, provider-mapped option IDs eligible for evaluation."""
    if SessionLocal is None:
        return
    async with SessionLocal() as session:
        result = await session.execute(text("""
          select distinct i.exchange_segment,i.security_id
          from public.target_tracking_sessions t
          join public.option_contracts c on c.id=t.option_contract_id
          join public.instruments i on i.id=c.instrument_id
          join public.market_data_providers p on p.id=i.provider_id
          where t.status='active' and p.code='DHANHQ' and i.exchange_segment='NSE_FNO'
            and i.is_active and c.is_active and not c.is_expired
          union
          select distinct i.exchange_segment,i.security_id
          from public.watchlist_items wi
          join public.watchlists w on w.id=wi.watchlist_id and w.user_id=wi.user_id
          join public.instruments i on i.id=wi.instrument_id
          where i.provider_id=(select id from public.market_data_providers where code='DHANHQ')
            and i.is_active and i.exchange_segment in ('IDX_I','NSE_EQ','NSE_FNO')
          union
          select distinct i.exchange_segment,i.security_id
          from public.watchlist_items wi
          join public.watchlists w on w.id=wi.watchlist_id and w.user_id=wi.user_id
          join public.option_contracts c on c.id=wi.option_contract_id
          join public.instruments i on i.id=c.instrument_id
          where i.provider_id=(select id from public.market_data_providers where code='DHANHQ')
            and i.is_active and c.is_active and not c.is_expired and i.exchange_segment='NSE_FNO'
        """))
        validated_pairs = [(row[0], str(row[1])) for row in result.all()]
        target_result = await session.execute(text("""
          select distinct i.exchange_segment,i.security_id
          from public.target_tracking_sessions t
          join public.option_contracts c on c.id=t.option_contract_id
          join public.instruments i on i.id=c.instrument_id
          join public.market_data_providers p on p.id=i.provider_id
          where t.status='active' and p.code='DHANHQ' and i.exchange_segment='NSE_FNO'
            and i.is_active and c.is_active and not c.is_expired
        """))
        target_pairs = [(row[0], str(row[1])) for row in target_result.all()]
    new_values = {f"{segment}:{security_id}" for segment, security_id in validated_pairs}
    target_values = {f"{segment}:{security_id}" for segment, security_id in target_pairs}
    old_values = {value.decode() if isinstance(value, bytes) else value for value in await redis.smembers(VALIDATED_SUBSCRIPTIONS)}
    pipeline = redis.pipeline(transaction=True)
    pipeline.delete(SUBSCRIPTIONS)
    pipeline.delete(VALIDATED_SUBSCRIPTIONS)
    if new_values:
        pipeline.sadd(VALIDATED_SUBSCRIPTIONS, *new_values)
    if target_values:
        pipeline.sadd(SUBSCRIPTIONS, *target_values)
    pipeline.expire(VALIDATED_SUBSCRIPTIONS, 15)
    pipeline.expire(SUBSCRIPTIONS, 15)
    await pipeline.execute()
    for pair in sorted(new_values - old_values):
        segment, security_id = pair.split(":", 1)
        await redis.publish("market:subscriptions:add", json.dumps({
            "exchange_segment": segment, "security_id": security_id, "source": "DHANHQ"}))


async def _process(redis, message_id: str, fields: dict) -> None:
    if SessionLocal is None:
        raise RuntimeError("Target monitoring database is unavailable")
    async def discard() -> None:
        await redis.xack(STREAM, GROUP, message_id)
        await redis.xdel(STREAM, message_id)

    raw = fields.get("quote")
    if isinstance(raw, bytes):
        raw = raw.decode()
    try:
        observation = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        log.warning("target_observation_invalid_json")
        await discard()
        return
    # Only raw frames written directly by the Dhan WebSocket ingest path are
    # eligible. REST cache data and browser messages never enter this stream.
    if observation.get("source") != "DHANHQ" or observation.get("exchange_segment") != "NSE_FNO":
        await discard()
        return
    if (not str(observation.get("security_id", "")).isdigit()
            or not isinstance(observation.get("exchange_timestamp"), str)
            or not isinstance(observation.get("received_at") or observation.get("ingested_at"), str)):
        log.warning("target_observation_invalid_fields")
        await discard()
        return
    async with SessionLocal() as session:
        await TargetStatusService(
            session, redis,
            stale_after_seconds=get_settings().market_data_stale_after_seconds,
        ).process_live_observation(observation)
        # TargetStatusService commits target_events before returning. The inbox
        # row commits before the per-user WebSocket notification is published.
        await AlertEngine().deliver_pending(session, redis, observation)
    await redis.xack(STREAM, GROUP, message_id)
    await redis.xdel(STREAM, message_id)


async def run_live_target_monitor(redis, *, stop_event: asyncio.Event | None = None) -> None:
    """Redis consumer-group worker with pending-message recovery and DB reconciliation."""
    if SessionLocal is None:
        log.error("target_monitor_disabled", reason="database_unconfigured")
        return
    consumer = f"{socket.gethostname()}-{os.getpid()}"
    try:
        await redis.xgroup_create(STREAM, GROUP, id="0-0", mkstream=True)
    except Exception as exc:
        if "BUSYGROUP" not in str(exc):
            raise
    refresh_at = 0.0
    claim_at = 0.0
    while stop_event is None or not stop_event.is_set():
        now = time.monotonic()
        if now >= refresh_at:
            try:
                await refresh_target_subscriptions(redis)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.error("target_subscription_refresh_failed", error_type=type(exc).__name__)
            refresh_at = now + 5
        try:
            await redis.set("market:target:worker:last_seen", datetime.now(timezone.utc).isoformat(), ex=30)
        except Exception:
            pass
        if now >= claim_at:
            try:
                claimed = await redis.xautoclaim(STREAM, GROUP, consumer, min_idle_time=30000, start_id="0-0", count=50)
                for message_id, fields in claimed[1]:
                    try:
                        await _process(redis, message_id, fields)
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        log.error("target_observation_processing_failed", error_type=type(exc).__name__)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("target_observation_recovery_failed", error_type=type(exc).__name__)
            claim_at = now + 10
        try:
            batches = await redis.xreadgroup(GROUP, consumer, {STREAM: ">"}, count=50, block=1000)
            for _, messages in batches:
                for message_id, fields in messages:
                    try:
                        await _process(redis, message_id, fields)
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        # Leave unacked; XAUTOCLAIM will retry after the lease.
                        log.error("target_observation_processing_failed", error_type=type(exc).__name__)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.error("target_observation_stream_failed", error_type=type(exc).__name__)
            await asyncio.sleep(1)

import asyncio
import json
import struct
from uuid import uuid4
from datetime import datetime, timezone

import structlog
from redis.asyncio import Redis

from app.config import Settings
from app.dhan.client import DhanRestClient
from app.dhan.config import DhanConfig
from app.dhan.provider import DhanProvider

log = structlog.get_logger(__name__)
DHAN_FEED_LEASE = "market:dhan:feed:lease"
TICKER_RESPONSE_CODE = 2
PREVIOUS_CLOSE_RESPONSE_CODE = 6
TICKER_PACKET_SIZE = 16
EXCHANGE_SEGMENTS = {
    0: "IDX_I", 1: "NSE_EQ", 2: "NSE_FNO", 3: "NSE_CURRENCY",
    4: "BSE_EQ", 5: "MCX_COMM", 7: "BSE_CURRENCY", 8: "BSE_FNO",
}


async def _validated_initial_instruments(settings: Settings) -> list[tuple[str, str]]:
    """Intersect deployment config with active Dhan master mappings before subscribing."""
    configured = settings.configured_dhan_instruments
    if not configured:
        return []
    from app.database import SessionLocal
    if SessionLocal is None:
        log.warning("dhan_configured_instruments_unvalidated", count=len(configured), reason="database_unavailable")
        return []
    from sqlalchemy import text
    async with SessionLocal() as session:
        result = await session.execute(text("""
          select i.exchange_segment,i.security_id
          from public.instruments i
          join public.market_data_providers p on p.id=i.provider_id
          where p.code='DHANHQ' and i.is_active
            and i.exchange_segment in ('IDX_I','NSE_EQ','NSE_FNO','NSE_CURRENCY','BSE_EQ','MCX_COMM','BSE_CURRENCY','BSE_FNO')
            and (i.expiry_date is null or i.expiry_date >= (now() at time zone 'Asia/Kolkata')::date)
        """))
        valid = {(row[0], str(row[1])) for row in result.all()}
    accepted = list(dict.fromkeys(pair for pair in configured if pair in valid))
    rejected = len(set(configured) - valid)
    if rejected:
        log.warning("dhan_configured_instruments_rejected", count=rejected,
                    reason="not_in_active_dhan_instrument_master")
    return accepted


async def _renew_feed_lease(redis: Redis, owner: str, lost: asyncio.Event) -> None:
    renew_script = "if redis.call('get', KEYS[1]) == ARGV[1] then return redis.call('expire', KEYS[1], 30) else return 0 end"
    while True:
        await asyncio.sleep(10)
        try:
            renewed = await redis.eval(renew_script, 1, DHAN_FEED_LEASE, owner)
            if not renewed:
                lost.set()
                return
        except asyncio.CancelledError:
            raise
        except Exception:
            lost.set()
            return


def decode_ticker_packet(packet: bytes) -> dict | None:
    """Decode the documented DhanHQ v2 ticker packet; reject unknown/malformed packets."""
    if len(packet) != TICKER_PACKET_SIZE:
        return None
    response_code = packet[0]
    if response_code != TICKER_RESPONSE_CODE:
        return None
    message_length = struct.unpack_from("<H", packet, 1)[0]
    if message_length != len(packet):
        return None
    security_id = struct.unpack_from("<I", packet, 4)[0]
    exchange_segment = EXCHANGE_SEGMENTS.get(packet[3])
    price = struct.unpack_from("<f", packet, 8)[0]
    exchange_epoch = struct.unpack_from("<i", packet, 12)[0]
    if not exchange_segment or security_id <= 0 or price <= 0 or exchange_epoch <= 0:
        return None
    return {
        "security_id": str(security_id),
        "exchange_segment": exchange_segment,
        "last_price": price,
        "exchange_timestamp": datetime.fromtimestamp(exchange_epoch, tz=timezone.utc).isoformat(),
    }


async def run_dhan_feed(settings: Settings, redis: Redis, provider: DhanProvider | None = None) -> None:
    """Connect and reconnect to Dhan; absent credentials never trigger a fallback provider."""
    try:
        configured_instruments = settings.configured_dhan_instruments
    except ValueError as exc:
        log.error("dhan_configuration_invalid", error=str(exc))
        return
    if not settings.dhan_credentials_configured:
        log.warning("dhan_feed_unconfigured", credentials=False, instrument_count=len(configured_instruments))
        return

    if provider is None:
        config = DhanConfig.from_settings(settings)
        provider = DhanProvider(config, DhanRestClient(config, redis_client=redis), redis_client=redis)
    retry_delay = 1.0
    while True:
        owner = uuid4().hex
        try:
            acquired = await redis.set(DHAN_FEED_LEASE, owner, nx=True, ex=30)
        except Exception as exc:
            log.error("dhan_feed_lease_unavailable", error_type=type(exc).__name__)
            await asyncio.sleep(5)
            continue
        if not acquired:
            await asyncio.sleep(5)
            continue
        lease_lost = asyncio.Event()
        lease_task = asyncio.create_task(_renew_feed_lease(redis, owner, lease_lost), name="dhan-feed-lease")
        stream = None
        try:
            instruments = await _validated_initial_instruments(settings)
            # Rehydrate validated option subscriptions on reconnect. The set is
            # maintained from active, instrument-master-confirmed sessions.
            try:
                active = await redis.smembers("market:subscriptions:validated")
                for value in active:
                    pair = value.decode() if isinstance(value, bytes) else value
                    segment, separator, security_id = pair.partition(":")
                    if separator and segment == "NSE_FNO" and security_id.isdigit() and int(security_id) > 0:
                        if (segment, security_id) not in instruments:
                            if len(instruments) >= 5000:
                                log.warning("dhan_feed_subscription_limit_reached", limit=5000)
                                break
                            instruments.append((segment, security_id))
            except Exception as exc:
                log.warning("dhan_target_subscriptions_unavailable", error_type=type(exc).__name__)
            log.info("dhan_feed_connecting", instrument_count=len(instruments))
            stream = provider.stream_market_data(instruments, mode=settings.dhan_feed_mode).__aiter__()
            while not lease_lost.is_set():
                quote_task = asyncio.create_task(anext(stream))
                lease_wait_task = asyncio.create_task(lease_lost.wait())
                done, pending = await asyncio.wait(
                    (quote_task, lease_wait_task), return_when=asyncio.FIRST_COMPLETED)
                for task in pending:
                    task.cancel()
                for task in pending:
                    try:
                        await task
                    except asyncio.CancelledError:
                        pass
                if lease_wait_task in done and lease_lost.is_set():
                    quote_task.cancel()
                    try:
                        await quote_task
                    except (asyncio.CancelledError, StopAsyncIteration):
                        pass
                    break
                try:
                    quote = quote_task.result()
                except StopAsyncIteration:
                    break
                key = f"market:quote:{quote['exchange_segment']}:{quote['security_id']}"
                encoded = json.dumps(quote)
                await redis.set(key, encoded)
                await redis.set("market:feed:last_received_at", quote["received_at"])
                await redis.set(f"market:feed:last_received_at:{quote['exchange_segment']}:{quote['security_id']}", quote["received_at"])
                await redis.publish("market:quotes", encoded)
                if quote.get("exchange_segment") == "NSE_FNO":
                    pair = f"{quote['exchange_segment']}:{quote['security_id']}"
                    try:
                        if await redis.sismember("market:target:subscriptions", pair):
                            # Redis Stream + consumer group separates socket ingest
                            # from DB work and retains unacknowledged observations.
                            await redis.xadd("market:target:observations", {"quote": encoded})
                    except Exception as exc:
                        log.error("target_observation_enqueue_failed", error_type=type(exc).__name__)
                        # Feed/cache remains healthy; monitor marks target sessions
                        # unverified until the durable monitoring path recovers.
            log.warning("dhan_feed_closed")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.error("dhan_feed_disconnected", error_type=type(exc).__name__)
        finally:
            if stream is not None:
                try:
                    await stream.aclose()
                except Exception:
                    pass
            lease_task.cancel()
            try:
                await lease_task
            except asyncio.CancelledError:
                pass
            try:
                await redis.eval(
                    "if redis.call('get', KEYS[1]) == ARGV[1] then return redis.call('del', KEYS[1]) else return 0 end",
                    1, DHAN_FEED_LEASE, owner)
            except Exception:
                pass
        await asyncio.sleep(retry_delay)
        retry_delay = min(30.0, retry_delay * 2)

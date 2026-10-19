import asyncio
import json
import struct
from datetime import datetime, timezone
from urllib.parse import urlencode

import structlog
import websockets
from redis.asyncio import Redis

from app.config import Settings

log = structlog.get_logger(__name__)
FEED_URL = "wss://api-feed.dhan.co"
TICKER_RESPONSE_CODE = 2
PREVIOUS_CLOSE_RESPONSE_CODE = 6
TICKER_PACKET_SIZE = 16
EXCHANGE_SEGMENTS = {
    0: "IDX_I", 1: "NSE_EQ", 2: "NSE_FNO", 3: "NSE_CURRENCY",
    4: "BSE_EQ", 5: "MCX_COMM", 7: "BSE_CURRENCY", 8: "BSE_FNO",
}


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


def _dhan_url(settings: Settings) -> str:
    query = urlencode({
        "version": "2",
        "token": settings.dhan_access_token,
        "clientId": settings.dhan_client_id,
        "authType": "2",
    })
    return f"{FEED_URL}?{query}"


async def run_dhan_feed(settings: Settings, redis: Redis) -> None:
    """Connect and reconnect to Dhan; absent credentials never trigger a fallback provider."""
    try:
        instruments = settings.configured_dhan_instruments
    except ValueError as exc:
        log.error("dhan_configuration_invalid", error=str(exc))
        return
    if not settings.dhan_credentials_configured or not instruments:
        log.warning("dhan_feed_unconfigured", credentials=bool(settings.dhan_client_id and settings.dhan_access_token), instrument_count=len(instruments))
        return

    grouped: dict[str, list[str]] = {}
    for segment, security_id in instruments:
        grouped.setdefault(segment, []).append(security_id)
    subscriptions = [
        (segment, grouped[segment][index:index + 100])
        for segment in grouped
        for index in range(0, len(grouped[segment]), 100)
    ]
    while True:
        try:
            async with websockets.connect(_dhan_url(settings), ping_interval=None, close_timeout=5) as socket:
                log.info("dhan_feed_connected", instrument_count=len(instruments))
                for segment, security_ids in subscriptions:
                    await socket.send(json.dumps({
                        "RequestCode": 15,
                        "InstrumentCount": len(security_ids),
                        "InstrumentList": [{"ExchangeSegment": segment, "SecurityId": security_id} for security_id in security_ids],
                    }))
                async for message in socket:
                    if isinstance(message, str):
                        log.warning("dhan_feed_text_frame_ignored")
                        continue
                    if message and message[0] == 50:
                        raise ConnectionError("DhanHQ sent its documented feed disconnect packet")
                    quote = decode_ticker_packet(message)
                    if quote is None:
                        log.debug("dhan_feed_packet_ignored", size=len(message))
                        continue
                    quote["source"] = "DHANHQ"
                    quote["received_at"] = datetime.now(timezone.utc).isoformat()
                    key = f"market:quote:{quote['exchange_segment']}:{quote['security_id']}"
                    await redis.set(key, json.dumps(quote))
                    await redis.publish("market:quotes", json.dumps(quote))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.error("dhan_feed_disconnected", error_type=type(exc).__name__)
            await asyncio.sleep(5)

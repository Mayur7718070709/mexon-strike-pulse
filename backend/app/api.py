import json
from datetime import datetime, timezone

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.security import HTTPAuthorizationCredentials
from jwt import InvalidTokenError

from app.auth import decode_supabase_token, require_user
from app.config import get_settings
from app.database import check_database
from app.dhan.monitor import DhanConnectionMonitor
from app.redis_client import check_redis

router = APIRouter()
integrations_router = APIRouter(prefix="/integrations", tags=["integrations"])
log = structlog.get_logger(__name__)


@integrations_router.get("/dhan/status")
async def dhan_status(request: Request, _user: dict = Depends(require_user)):
    monitor: DhanConnectionMonitor | None = getattr(request.app.state, "dhan_monitor", None)
    if monitor is None:
        return {"status": "DATA UNAVAILABLE", "connected": False, "checked_at": datetime.now(timezone.utc), "message": "Dhan integration is unavailable."}
    return await monitor.status()


router.include_router(integrations_router)


@router.get("/health", tags=["system"])
async def health():
    database_ok, redis_ok = await check_database(), await check_redis()
    return {
        "status": "ok" if database_ok and redis_ok else "degraded",
        "dependencies": {"postgres": "ok" if database_ok else "unavailable", "redis": "ok" if redis_ok else "unavailable"},
        "timestamp": datetime.now(timezone.utc),
    }


@router.get("/market/status", tags=["market data"])
async def market_status(request: Request, _user: dict = Depends(require_user)):
    settings = get_settings()
    redis = request.app.state.redis
    try:
        instruments = settings.configured_dhan_instruments
    except ValueError as exc:
        raise HTTPException(status_code=503, detail="Market data configuration is invalid") from exc
    if not instruments:
        return {"status": "DATA UNAVAILABLE", "source": "DHANHQ", "quotes": [], "message": "No Dhan instruments are configured."}
    quotes = []
    now = datetime.now(timezone.utc)
    for segment, security_id in instruments:
        try:
            raw = await redis.get(f"market:quote:{segment}:{security_id}")
        except Exception as exc:
            log.error("market_cache_unavailable", error_type=type(exc).__name__)
            return {"status": "DATA UNAVAILABLE", "source": "DHANHQ", "quotes": [], "message": "Market data cache is unavailable."}
        if raw is None:
            continue
        try:
            quote = json.loads(raw)
            received_at = datetime.fromisoformat(quote["received_at"])
        except (ValueError, KeyError, TypeError):
            log.error("market_cache_packet_invalid", exchange_segment=segment, security_id=security_id)
            continue
        age = (now - received_at).total_seconds()
        quote["status"] = "LIVE" if age <= settings.market_data_stale_after_seconds else "DATA STALE"
        quotes.append(quote)
    if not quotes:
        return {"status": "DATA UNAVAILABLE", "source": "DHANHQ", "quotes": [], "message": "Waiting for a live DhanHQ packet."}
    status = "LIVE" if all(q["status"] == "LIVE" for q in quotes) else "DATA STALE"
    return {"status": status, "source": "DHANHQ", "quotes": quotes}


@router.websocket("/ws/market")
async def market_stream(websocket: WebSocket):
    """Stream Dhan packets distributed by Redis; no market values are generated here."""
    token_protocol = next((value for value in websocket.scope.get("subprotocols", []) if value.startswith("access-token.")), None)
    if token_protocol is None:
        await websocket.close(code=1008, reason="Supabase session required")
        return
    try:
        await decode_supabase_token(token_protocol.removeprefix("access-token."))
    except (InvalidTokenError, HTTPException):
        await websocket.close(code=1008, reason="Invalid Supabase session")
        return
    await websocket.accept(subprotocol="market-pulse")
    pubsub = websocket.app.state.redis.pubsub()
    await pubsub.subscribe("market:quotes")
    try:
        while True:
            message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=20)
            if message and message.get("data"):
                await websocket.send_text(message["data"])
            else:
                await websocket.send_json({"status": "DATA UNAVAILABLE", "source": "DHANHQ", "quotes": []})
    except WebSocketDisconnect:
        pass
    except Exception as exc:
        log.error("market_stream_dependency_error", error_type=type(exc).__name__)
        await websocket.close(code=1011, reason="Market stream unavailable")
    finally:
        await pubsub.unsubscribe("market:quotes")
        await pubsub.aclose()

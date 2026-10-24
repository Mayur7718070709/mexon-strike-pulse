import json
from datetime import date, datetime, timezone
from decimal import Decimal
from uuid import UUID

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.security import HTTPAuthorizationCredentials
from jwt import InvalidTokenError
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.auth import decode_supabase_token, require_admin, require_user
from app.analysis.past_present_next_future import PastPresentNextFutureService
from app.scanner.service import ScannerService
from app.config import get_settings
from app.database import SessionLocal, check_database
from app.dhan.errors import DhanAuthenticationError, DhanInvalidResponse, DhanRateLimitError, DhanRequestError, DhanUnavailableError
from app.dhan.monitor import DhanConnectionMonitor
from app.dhan.health import ProviderHealthService
from app.redis_client import check_redis
from app.instruments.repository import InstrumentRepository
from app.instruments.option_chain import OptionChainService
from app.instruments.service import ExpiryService, InstrumentMasterError, InstrumentSyncService
from app.historical.coverage import HistoricalCoverageService
from app.historical.repository import HistoricalRepository
from app.historical.service import HistoricalDataService
from app.expired_options.analyzer import ExpiredOptionsAnalyzer
from app.expired_options.coverage import ExpiredOptionsCoverageService
from app.expired_options.repository import ExpiredOptionsRepository
from app.expired_options.service import ExpiredOptionsService
from app.targets.status import TargetDataUnavailable, TargetStatusService
from app.watchlists.schemas import TargetStartRequest, WatchlistCreateRequest, WatchlistItemCreateRequest
from app.watchlists.service import WatchlistService

router = APIRouter()
integrations_router = APIRouter(prefix="/integrations", tags=["integrations"])
log = structlog.get_logger(__name__)


async def instrument_session():
    if SessionLocal is None:
        raise HTTPException(status_code=503, detail="Instrument database is unavailable")
    async with SessionLocal() as session:
        yield session


class HistoricalBackfillRequest(BaseModel):
    underlying: str | None = None
    instrument: str | None = None
    interval: str
    from_date: date
    to_date: date

    @model_validator(mode="after")
    def validate_request(self):
        if bool(self.underlying) == bool(self.instrument):
            raise ValueError("Provide exactly one of underlying or instrument")
        if self.interval not in {"1", "5", "15", "25", "60", "1d"}:
            raise ValueError("interval must be one of 1, 5, 15, 25, 60, or 1d")
        if self.from_date >= self.to_date:
            raise ValueError("from_date must be earlier than to_date; to_date is exclusive")
        for field in ("underlying", "instrument"):
            value = getattr(self, field)
            if value is not None and (not value.strip() or len(value) > 80):
                raise ValueError(f"{field} must be a non-empty symbol up to 80 characters")
        return self


class ExpiredOptionsBackfillRequest(BaseModel):
    underlying: str
    interval: str
    expiry_flag: str
    expiry_code: int
    strike_selections: list[str] = Field(default_factory=lambda: ["ATM"])
    option_types: list[str] = Field(default_factory=lambda: ["CALL", "PUT"])
    from_date: date
    to_date: date

    @model_validator(mode="after")
    def validate_request(self):
        if not self.underlying.strip() or len(self.underlying) > 40:
            raise ValueError("underlying must be a non-empty symbol up to 40 characters")
        if self.from_date >= self.to_date:
            raise ValueError("from_date must be earlier than to_date; Dhan's toDate is exclusive")
        return self


def watchlist_service(session, request: Request) -> WatchlistService:
    return WatchlistService(session, request.app.state.redis,
                            stale_after_seconds=get_settings().market_data_stale_after_seconds)


@router.get("/watchlists", tags=["watchlists"])
async def list_watchlists(request: Request, user: dict = Depends(require_user), session=Depends(instrument_session)):
    return {"watchlists": await watchlist_service(session, request).list_for_user(UUID(user["id"]))}


@router.post("/watchlists", status_code=201, tags=["watchlists"])
async def create_watchlist(payload: WatchlistCreateRequest, user: dict = Depends(require_user),
                           session=Depends(instrument_session)):
    try:
        return await WatchlistService(session, None).create(
            user_id=UUID(user["id"]), name=payload.name,
            description=payload.description or None, is_default=payload.is_default)
    except IntegrityError as exc:
        raise HTTPException(status_code=409, detail="A watchlist with this name already exists") from exc


@router.post("/watchlists/{watchlist_id}/items", status_code=201, tags=["watchlists"])
async def add_watchlist_item(watchlist_id: UUID, payload: WatchlistItemCreateRequest, request: Request,
                             user: dict = Depends(require_user), session=Depends(instrument_session)):
    try:
        return await watchlist_service(session, request).add_item(
            user_id=UUID(user["id"]), watchlist_id=watchlist_id,
            instrument_id=payload.instrument_id, option_contract_id=payload.option_contract_id,
            underlying=payload.underlying, expiry=payload.expiry, strike=payload.strike,
            option_type=payload.option_type, note=payload.note or None)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail="Watchlist not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.delete("/watchlists/{watchlist_id}/items/{item_id}", tags=["watchlists"])
async def remove_watchlist_item(watchlist_id: UUID, item_id: UUID, user: dict = Depends(require_user),
                                session=Depends(instrument_session)):
    removed = await WatchlistService(session, None).remove_item(
        user_id=UUID(user["id"]), watchlist_id=watchlist_id, item_id=item_id)
    if not removed:
        raise HTTPException(status_code=404, detail="Watchlist item not found")
    return {"status": "removed", "item_id": str(item_id)}


@router.post("/targets/start", tags=["targets"])
async def start_target_tracking(payload: TargetStartRequest, request: Request,
                                user: dict = Depends(require_user), session=Depends(instrument_session)):
    provider = getattr(request.app.state, "market_data_provider", None)
    if provider is None or not provider.config.is_configured:
        raise HTTPException(status_code=503, detail="Dhan live feed is unavailable; no reference was captured")
    service = TargetStatusService(session, request.app.state.redis,
                                  stale_after_seconds=get_settings().market_data_stale_after_seconds)
    try:
        return await service.start_session(
            user_id=UUID(user["id"]), request_id=payload.request_id,
            underlying=payload.underlying, expiry=payload.expiry, strike=payload.strike,
            option_type=payload.option_type,
            confirmed_reference_premium=payload.confirmed_reference_premium,
            confirmed_reference_timestamp=payload.confirmed_reference_timestamp)
    except TargetDataUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/targets", tags=["targets"])
async def list_targets(request: Request, user: dict = Depends(require_user), session=Depends(instrument_session)):
    service = TargetStatusService(session, request.app.state.redis,
                                  stale_after_seconds=get_settings().market_data_stale_after_seconds)
    return {"targets": await service.list_sessions(UUID(user["id"]))}


@router.get("/alerts", tags=["alerts"])
async def list_alerts(user: dict = Depends(require_user), session=Depends(instrument_session),
                      limit: int = Query(default=50, ge=1, le=100), before: datetime | None = None):
    count_result = await session.execute(text("""
      select count(*) from public.user_notifications where user_id=:user_id and read_at is null
    """), {"user_id": UUID(user["id"])})
    unread_count = count_result.scalar_one()
    result = await session.execute(text("""
      select id,notification_type,title,body,payload,read_at,created_at
      from public.user_notifications
      where user_id=:user_id and (:before is null or created_at < :before)
      order by created_at desc limit :limit
    """), {"user_id": UUID(user["id"]), "before": before, "limit": limit})
    rows = [dict(row) for row in result.mappings()]
    return {"notifications": rows, "unread_count": unread_count}


@router.post("/alerts/{notification_id}/read", tags=["alerts"])
async def mark_alert_read(notification_id: UUID, user: dict = Depends(require_user),
                          session=Depends(instrument_session)):
    result = await session.execute(text("""
      update public.user_notifications set read_at=coalesce(read_at,now()),updated_at=now()
      where id=:id and user_id=:user_id returning id,read_at
    """), {"id": notification_id, "user_id": UUID(user["id"])})
    row = result.mappings().one_or_none()
    if row is None:
        await session.rollback()
        raise HTTPException(status_code=404, detail="Alert not found")
    await session.commit()
    return {"id": str(row["id"]), "read_at": row["read_at"]}


@router.post("/targets/{target_id}/stop", tags=["targets"])
async def stop_target_tracking(target_id: UUID, user: dict = Depends(require_user),
                               session=Depends(instrument_session)):
    stopped = await TargetStatusService(session, None).cancel(session_id=target_id, user_id=UUID(user["id"]))
    if not stopped:
        raise HTTPException(status_code=404, detail="Active target session not found")
    return {"target_id": str(target_id), "status": "stopped", "message": "Tracking stopped by explicit user action."}


@integrations_router.get("/dhan/status")
async def dhan_status(request: Request, _user: dict = Depends(require_user)):
    health_service: ProviderHealthService | None = getattr(request.app.state, "provider_health_service", None)
    if health_service is None:
        return {"status": "DATA UNAVAILABLE", "connected": False, "checked_at": datetime.now(timezone.utc), "message": "Dhan integration is unavailable."}
    result = await health_service.get_status()
    try:
        last_received = await request.app.state.redis.get("market:feed:last_received_at")
        result["last_received_at"] = last_received.decode() if isinstance(last_received, bytes) else last_received
        monitor_seen = await request.app.state.redis.get("market:target:worker:last_seen")
        monitor_seen = monitor_seen.decode() if isinstance(monitor_seen, bytes) else monitor_seen
        result["target_monitor_worker"] = "RUNNING" if monitor_seen else "DATA UNAVAILABLE"
        if result["last_received_at"]:
            received_at = datetime.fromisoformat(result["last_received_at"].replace("Z", "+00:00"))
            age = max(0.0, (datetime.now(timezone.utc) - received_at).total_seconds())
            result["last_received_age_seconds"] = age
            result["feed_data_quality"] = "LIVE" if age <= get_settings().market_data_stale_after_seconds else "DATA STALE"
        else:
            result["last_received_age_seconds"] = None
            result["feed_data_quality"] = "DATA UNAVAILABLE"
    except Exception as exc:
        log.warning("dhan_feed_timestamp_unavailable", error_type=type(exc).__name__)
        result["last_received_at"] = None
    return result


router.include_router(integrations_router)


@router.get("/instruments", tags=["instruments"])
async def list_instruments(
    search: str | None = Query(default=None, min_length=1, max_length=80),
    underlying: str | None = Query(default=None, min_length=1, max_length=40),
    include_inactive: bool = False,
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0, le=100000),
    _user: dict = Depends(require_user),
    session=Depends(instrument_session),
):
    rows = await InstrumentRepository(session).list_instruments(
        symbol=underlying.strip().upper() if underlying else None,
        search=search, include_inactive=include_inactive, limit=limit, offset=offset,
    )
    return {"status": "AVAILABLE" if rows else "UNAVAILABLE", "source": "DHANHQ", "count": len(rows), "instruments": rows}


@router.get("/instruments/{symbol}", tags=["instruments"])
async def get_instrument(symbol: str, _user: dict = Depends(require_user), session=Depends(instrument_session)):
    normalized = symbol.strip().upper()
    rows = await InstrumentRepository(session).list_instruments(
        symbol=normalized, search=None, include_inactive=True, limit=500, offset=0,
    )
    if not rows:
        return {"status": "UNAVAILABLE", "available": False, "symbol": normalized,
                "source": "DHANHQ", "instruments": [], "message": "No provider-confirmed Dhan mapping is available for this symbol."}
    return {"status": "AVAILABLE", "available": any(row["is_active"] and not row["is_expired"] for row in rows),
            "symbol": normalized, "source": "DHANHQ", "instruments": rows}


@router.get("/options/{underlying}/expiries", tags=["instruments"])
async def list_option_expiries(underlying: str, request: Request, _user: dict = Depends(require_user), session=Depends(instrument_session)):
    provider = getattr(request.app.state, "market_data_provider", None)
    if provider is None:
        return {"status": "UNAVAILABLE", "available": False, "underlying": underlying.upper(), "source": "DHANHQ", "expiries": []}
    try:
        return await ExpiryService(provider, InstrumentRepository(session)).list_expiries(underlying)
    except DhanRateLimitError as exc:
        raise HTTPException(status_code=429, detail=str(exc)) from exc
    except (DhanUnavailableError, DhanAuthenticationError) as exc:
        raise HTTPException(status_code=503, detail="Dhan expiry data is currently unavailable") from exc
    except (DhanInvalidResponse, DhanRequestError, InstrumentMasterError, ValueError) as exc:
        await session.rollback()
        log.error("dhan_expiry_lookup_failed", underlying=underlying.upper(), error_type=type(exc).__name__)
        raise HTTPException(status_code=502, detail="Dhan returned an invalid expiry response") from exc


@router.get("/options/underlyings", tags=["options"])
async def list_option_underlyings(_user: dict = Depends(require_user), session=Depends(instrument_session)):
    underlyings = await InstrumentRepository(session).list_option_underlyings()
    return {"status": "AVAILABLE" if underlyings else "DATA UNAVAILABLE", "source": "DHANHQ", "underlyings": underlyings}


@router.get("/options/{underlying}/chain", tags=["options"])
async def get_option_chain(
    underlying: str,
    request: Request,
    expiry: date = Query(..., description="ISO expiry date confirmed by synchronized Dhan instrument data"),
    _user: dict = Depends(require_user),
    session=Depends(instrument_session),
):
    provider = getattr(request.app.state, "market_data_provider", None)
    if provider is None:
        return {"status": "DATA UNAVAILABLE", "available": False, "source": "DHANHQ", "strikes": [], "message": "Dhan option-chain provider is unavailable."}
    try:
        return await OptionChainService(
            provider, InstrumentRepository(session), request.app.state.redis,
            stale_after_seconds=get_settings().market_data_stale_after_seconds,
        ).get_chain(underlying, expiry)
    except DhanRateLimitError as exc:
        raise HTTPException(status_code=429, detail="Dhan option-chain rate limit reached; retry shortly") from exc
    except (DhanUnavailableError, DhanAuthenticationError) as exc:
        raise HTTPException(status_code=503, detail="Dhan option-chain data is currently unavailable") from exc
    except DhanRequestError as exc:
        raise HTTPException(status_code=422, detail="Dhan no longer confirms this option-chain request or expiry") from exc
    except DhanInvalidResponse as exc:
        log.error("dhan_option_chain_invalid_response", underlying=underlying.upper(), error_type=type(exc).__name__)
        raise HTTPException(status_code=502, detail="Dhan returned an invalid option-chain response") from exc


@router.get("/scanner/opportunities", tags=["scanner"])
async def scanner_opportunities(
    request: Request,
    underlying: str = Query(..., min_length=1, max_length=40),
    expiry: date = Query(..., description="An active expiry confirmed by the Dhan instrument master"),
    minimum_premium: Decimal | None = Query(default=None, gt=0),
    maximum_premium: Decimal | None = Query(default=None, gt=0),
    minimum_volume: int | None = Query(default=None, ge=0),
    minimum_oi: int | None = Query(default=None, ge=0),
    minimum_change_in_oi: int | None = Query(default=None),
    minimum_iv: float | None = Query(default=None, ge=0),
    maximum_iv: float | None = Query(default=None, ge=0),
    maximum_distance_pct: float | None = Query(default=None, ge=0, le=100),
    maximum_spread_pct: float | None = Query(default=None, ge=0, le=100),
    minimum_liquidity_quantity: int | None = Query(default=None, ge=0),
    max_data_age_seconds: int = Query(default=15, ge=1, le=60),
    minimum_score: float = Query(default=60, ge=0, le=100),
    minimum_score_coverage_pct: float = Query(default=50, ge=1, le=100),
    limit: int = Query(default=300, ge=1, le=1000),
    offset: int = Query(default=0, ge=0, le=5000),
    _user: dict = Depends(require_user),
    session=Depends(instrument_session),
):
    if minimum_premium is not None and maximum_premium is not None and minimum_premium > maximum_premium:
        raise HTTPException(status_code=422, detail="minimum_premium cannot exceed maximum_premium")
    if minimum_iv is not None and maximum_iv is not None and minimum_iv > maximum_iv:
        raise HTTPException(status_code=422, detail="minimum_iv cannot exceed maximum_iv")
    settings = get_settings()
    service = ScannerService(session, getattr(request.app.state, "market_data_provider", None),
                             request.app.state.redis, stale_after_seconds=settings.market_data_stale_after_seconds)
    filters = {"minimum_premium": minimum_premium, "maximum_premium": maximum_premium,
               "minimum_volume": minimum_volume, "minimum_oi": minimum_oi,
               "minimum_change_in_oi": minimum_change_in_oi, "minimum_iv": minimum_iv,
               "maximum_iv": maximum_iv, "maximum_distance_pct": maximum_distance_pct,
               "maximum_spread_pct": maximum_spread_pct,
               "minimum_liquidity_quantity": minimum_liquidity_quantity,
               "max_data_age_seconds": min(max_data_age_seconds, settings.market_data_stale_after_seconds),
               "minimum_score": minimum_score,
               "minimum_score_coverage_pct": minimum_score_coverage_pct, "limit": limit}
    try:
        return await service.opportunities(underlying=underlying, expiry=expiry, filters=filters, offset=offset)
    except DhanRateLimitError as exc:
        raise HTTPException(status_code=429, detail="Dhan option-chain rate limit reached; retry shortly") from exc
    except (DhanUnavailableError, DhanAuthenticationError) as exc:
        raise HTTPException(status_code=503, detail="Dhan option-chain data is currently unavailable") from exc
    except (DhanRequestError, DhanInvalidResponse) as exc:
        log.error("dhan_scanner_request_failed", underlying=underlying.upper(), expiry=expiry.isoformat(), error_type=type(exc).__name__)
        raise HTTPException(status_code=502, detail="Dhan returned invalid or unavailable scanner data") from exc


@router.get("/analysis/past-present-next-future", tags=["analysis"])
async def past_present_next_future(
    request: Request,
    underlying: str = Query(..., min_length=1, max_length=40),
    strike: Decimal = Query(..., gt=0),
    option_type: str = Query(..., pattern="^(CALL|PUT|call|put)$"),
    past_expiry: date = Query(...),
    present_expiry: date = Query(...),
    next_expiry: date = Query(...),
    future_expiry: date = Query(...),
    past_from: date = Query(...),
    past_to: date = Query(...),
    _user: dict = Depends(require_user),
    session=Depends(instrument_session),
):
    provider = getattr(request.app.state, "market_data_provider", None)
    service = PastPresentNextFutureService(
        session, provider, request.app.state.redis,
        stale_after_seconds=get_settings().market_data_stale_after_seconds,
    )
    try:
        result = await service.analyze(underlying=underlying, strike=strike, option_type=option_type,
                                       past_expiry=past_expiry, present_expiry=present_expiry,
                                       next_expiry=next_expiry, future_expiry=future_expiry,
                                       past_from=past_from, past_to=past_to)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return result


@router.get("/target-sessions", tags=["targets"])
async def list_target_sessions(request: Request, user: dict = Depends(require_user), session=Depends(instrument_session)):
    service = TargetStatusService(session, request.app.state.redis,
                                  stale_after_seconds=get_settings().market_data_stale_after_seconds)
    return {"sessions": await service.list_sessions(UUID(user["id"]))}


@router.get("/target-sessions/{session_id}", tags=["targets"])
async def get_target_session(session_id: UUID, request: Request,
                             user: dict = Depends(require_user), session=Depends(instrument_session)):
    service = TargetStatusService(session, request.app.state.redis,
                                  stale_after_seconds=get_settings().market_data_stale_after_seconds)
    result = await service.get_session(session_id=session_id, user_id=UUID(user["id"]))
    if result is None:
        raise HTTPException(status_code=404, detail="Target tracking session not found")
    return result


@router.post("/target-sessions/{session_id}/cancel", tags=["targets"])
async def cancel_target_session(session_id: UUID, user: dict = Depends(require_user),
                                session=Depends(instrument_session)):
    canceled = await TargetStatusService(session, None).cancel(session_id=session_id, user_id=UUID(user["id"]))
    if not canceled:
        raise HTTPException(status_code=404, detail="Active target tracking session not found")
    return {"session_id": str(session_id), "status": "canceled", "message": "Tracking was stopped by explicit user action."}


@router.post("/admin/instruments/sync", tags=["admin", "instruments"])
async def sync_instruments(request: Request, _admin: dict = Depends(require_admin), session=Depends(instrument_session)):
    provider = getattr(request.app.state, "market_data_provider", None)
    if provider is None:
        raise HTTPException(status_code=503, detail="Dhan instrument master provider is unavailable")
    try:
        return await InstrumentSyncService(provider, session).synchronize()
    except DhanRateLimitError as exc:
        await session.rollback()
        log.error("dhan_instrument_sync_failed", error_type=type(exc).__name__)
        raise HTTPException(status_code=429, detail="Dhan instrument master rate limit reached") from exc
    except (DhanUnavailableError, DhanAuthenticationError) as exc:
        await session.rollback()
        log.error("dhan_instrument_sync_failed", error_type=type(exc).__name__)
        raise HTTPException(status_code=503, detail="Dhan instrument master is currently unavailable") from exc
    except Exception as exc:
        await session.rollback()
        log.error("dhan_instrument_sync_failed", error_type=type(exc).__name__)
        raise HTTPException(status_code=502, detail="Dhan instrument master synchronization failed") from exc


@router.post("/admin/historical/backfill", status_code=202, tags=["admin", "historical"])
async def backfill_historical(payload: HistoricalBackfillRequest, request: Request,
                              _admin: dict = Depends(require_admin), session=Depends(instrument_session)):
    provider = getattr(request.app.state, "market_data_provider", None)
    if provider is None or not provider.config.is_configured:
        raise HTTPException(status_code=503, detail="Dhan credentials/provider are unavailable; no historical request was made")
    try:
        return await HistoricalDataService(session).submit(
            user_id=UUID(_admin["id"]), underlying=payload.underlying, instrument=payload.instrument,
            interval=payload.interval, from_date=payload.from_date, to_date=payload.to_date,
            redis_url=get_settings().redis_url,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except RuntimeError as exc:
        log.error("historical_backfill_enqueue_failed", error_type=type(exc).__name__)
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.get("/admin/historical/jobs/{job_id}", tags=["admin", "historical"])
async def historical_job_status(job_id: UUID, _admin: dict = Depends(require_admin), session=Depends(instrument_session)):
    job = await HistoricalRepository(session).get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Historical ingestion job not found")
    return {
        "job_id": str(job["id"]), "status": job["status"], "timeframe": job["timeframe"],
        "requested_range": {"from": job["range_start"], "to_exclusive": job["range_end"]},
        "sample_verified": job["sample_verified"], "chunks_total": job["chunks_total"],
        "chunks_completed": job["chunks_completed"], "chunks_failed": job["chunks_failed"],
        "rows_received": job["rows_received"], "rows_upserted": job["rows_upserted"],
        "failures": [{"range_start": c["range_start"], "range_end": c["range_end"], "error_code": c["error_code"], "error_message": c["error_message"]}
                     for c in job["chunks"] if c["status"] in {"failed", "partial"}],
        "chunks": [{"range_start": c["range_start"], "range_end": c["range_end"], "status": c["status"],
                    "rows_received": c["rows_received"], "rows_upserted": c["rows_upserted"],
                    "invalid_rows": c["invalid_rows"], "duplicate_rows": c["duplicate_rows"]} for c in job["chunks"]],
        "coverage": job["coverage_report"], "created_at": job["created_at"],
        "updated_at": job["updated_at"], "completed_at": job["completed_at"],
    }


@router.get("/admin/historical/coverage", tags=["admin", "historical"])
async def historical_coverage(interval: str = Query(..., pattern="^(1|5|15|25|60|1d)$"),
                              underlying: str | None = Query(default=None, min_length=1, max_length=80),
                              instrument: str | None = Query(default=None, min_length=1, max_length=80),
                              _admin: dict = Depends(require_admin), session=Depends(instrument_session)):
    if bool(underlying) == bool(instrument):
        raise HTTPException(status_code=422, detail="Provide exactly one of underlying or instrument")
    mapping = await HistoricalRepository(session).resolve_instrument(underlying=underlying, instrument=instrument)
    if mapping is None:
        raise HTTPException(status_code=404, detail="No active Dhan instrument-master mapping matches this symbol")
    return await HistoricalCoverageService(session).report(
        mapping["instrument_id"], mapping["provider_id"], mapping["symbol"], interval,
    )


@router.post("/admin/expired-options/backfill", status_code=202, tags=["admin", "expired options"])
async def backfill_expired_options(payload: ExpiredOptionsBackfillRequest, request: Request,
                                   _admin: dict = Depends(require_admin), session=Depends(instrument_session)):
    provider = getattr(request.app.state, "market_data_provider", None)
    if provider is None or not provider.config.is_configured:
        raise HTTPException(status_code=503, detail="Dhan credentials/provider are unavailable; no request was made")
    try:
        result = await ExpiredOptionsService(session).submit(
            user_id=UUID(_admin["id"]), underlying=payload.underlying,
            interval=payload.interval, expiry_flag=payload.expiry_flag,
            expiry_code=payload.expiry_code, strikes=payload.strike_selections,
            option_types=payload.option_types, from_date=payload.from_date,
            to_date=payload.to_date, redis_url=get_settings().redis_url)
        if result.get("status") == "UNSUPPORTED":
            return result
        return result
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except RuntimeError as exc:
        log.error("expired_options_backfill_enqueue_failed", error_type=type(exc).__name__)
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.get("/admin/expired-options/jobs/{job_id}", tags=["admin", "expired options"])
async def expired_options_job_status(job_id: UUID, _admin: dict = Depends(require_admin), session=Depends(instrument_session)):
    job = await ExpiredOptionsRepository(session).get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Expired-options ingestion job not found")
    chunks = job["chunks"]
    return {
        "job_id": str(job["id"]), "status": job["status"], "source": "DHANHQ",
        "underlying": (job.get("metadata") or {}).get("symbol"), "interval": job["interval"],
        "requested_range": {"from": job["range_start"], "to_exclusive": job["range_end"]},
        "expiry_selector": {"expiry_flag": job["expiry_flag"], "expiry_code": job["expiry_code"]},
        "calendar_expiry_date": None, "sample_verified": job["sample_verified"],
        "chunks_total": job["chunks_total"], "chunks_completed": job["chunks_completed"],
        "chunks_failed": job["chunks_failed"], "rows_received": job["rows_received"],
        "rows_upserted": job["rows_upserted"], "error_code": job["error_code"],
        "error_message": job["error_message"], "coverage": job["coverage_report"],
        "failures": [{"from": chunk["range_start"], "to_exclusive": chunk["range_end"],
                      "strike_selection": chunk["strike_selection"], "option_type": chunk["option_type"],
                      "error_code": chunk["error_code"], "error_message": chunk["error_message"]}
                     for chunk in chunks if chunk["status"] in {"failed", "partial"}],
        "chunks": [{"from": chunk["range_start"], "to_exclusive": chunk["range_end"],
                    "strike_selection": chunk["strike_selection"], "option_type": chunk["option_type"],
                    "is_sample": chunk["is_sample"], "status": chunk["status"],
                    "rows_received": chunk["rows_received"], "rows_upserted": chunk["rows_upserted"],
                    "invalid_rows": chunk["invalid_rows"]} for chunk in chunks],
        "created_at": job["created_at"], "updated_at": job["updated_at"], "completed_at": job["completed_at"],
    }


@router.get("/admin/expired-options/coverage", tags=["admin", "expired options"])
async def expired_options_coverage(
    underlying: str = Query(..., min_length=1, max_length=40),
    interval: str = Query(..., pattern="^(1|5|15|25|60)$"),
    expiry_flag: str = Query(..., pattern="^(WEEK|MONTH)$"),
    expiry_code: int = Query(..., ge=0, le=2),
    strike_selection: str | None = Query(default=None, max_length=8),
    option_type: str | None = Query(default=None, pattern="^(CALL|PUT)$"),
    from_date: date | None = None,
    to_date: date | None = None,
    _admin: dict = Depends(require_admin), session=Depends(instrument_session),
):
    if from_date is not None and to_date is not None and from_date >= to_date:
        raise HTTPException(status_code=422, detail="from_date must be earlier than to_date; to_date is exclusive")
    mapping = await ExpiredOptionsRepository(session).resolve_underlying(underlying)
    if mapping is None:
        return {"status": "UNSUPPORTED", "available": False, "source": "DHANHQ", "underlying": underlying.upper(), "observations": 0}
    report = await ExpiredOptionsCoverageService(session).report(
        mapping["underlying_id"], mapping["provider_id"], mapping["root_symbol"], interval,
        expiry_flag, expiry_code, strike_selection, option_type,
        datetime.combine(from_date, datetime.min.time(), timezone.utc) if from_date else None,
        datetime.combine(to_date, datetime.min.time(), timezone.utc) if to_date else None)
    return {"status": "AVAILABLE" if report["observations"] else "NO OBSERVATIONS", **report}


@router.get("/admin/expired-options/jobs/{job_id}/analysis", tags=["admin", "expired options"])
async def expired_options_analysis(job_id: UUID, _admin: dict = Depends(require_admin), session=Depends(instrument_session)):
    repository = ExpiredOptionsRepository(session)
    job = await repository.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Expired-options ingestion job not found")
    rows = await repository.observations_for_job(job)
    analyses = ExpiredOptionsAnalyzer.analyze_groups(rows)
    return {"status": "AVAILABLE" if analyses else "NO OBSERVATIONS", "source": "DHANHQ",
            "underlying": (job.get("metadata") or {}).get("symbol"), "expiry_selector": {
                "expiry_flag": job["expiry_flag"], "expiry_code": job["expiry_code"]},
            "calendar_expiry_date": None, "analysis": analyses,
            "method": "per source date, actual strike, relative selector and side: first stored candle open as entry; maximum same-date candle high divided by entry; only stored provider observations are used"}


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
        user = await decode_supabase_token(token_protocol.removeprefix("access-token."))
    except (InvalidTokenError, HTTPException):
        await websocket.close(code=1008, reason="Invalid Supabase session")
        return
    await websocket.accept(subprotocol="market-pulse")
    pubsub = websocket.app.state.redis.pubsub()
    await pubsub.subscribe("market:quotes")
    try:
        while True:
            message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=20)
            if user.get("expires_at") is not None and datetime.now(timezone.utc).timestamp() >= float(user["expires_at"]):
                await websocket.close(code=1008, reason="Supabase session expired")
                break
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


@router.websocket("/ws/alerts")
async def alert_stream(websocket: WebSocket):
    """Only stream the inbox belonging to the Supabase identity in this socket token."""
    token_protocol = next((value for value in websocket.scope.get("subprotocols", [])
                           if value.startswith("access-token.")), None)
    if token_protocol is None:
        await websocket.close(code=1008, reason="Supabase session required")
        return
    try:
        user = await decode_supabase_token(token_protocol.removeprefix("access-token."))
        user_id = UUID(user["id"])
    except (InvalidTokenError, HTTPException, ValueError):
        await websocket.close(code=1008, reason="Invalid Supabase session")
        return
    await websocket.accept(subprotocol="market-pulse")
    channel = f"market-pulse:alerts:{user_id}"
    pubsub = websocket.app.state.redis.pubsub()
    await pubsub.subscribe(channel)
    try:
        while True:
            message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=20)
            if user.get("expires_at") is not None and datetime.now(timezone.utc).timestamp() >= float(user["expires_at"]):
                await websocket.close(code=1008, reason="Supabase session expired")
                break
            if message and message.get("data"):
                data = message["data"]
                await websocket.send_text(data.decode() if isinstance(data, bytes) else data)
            else:
                # WebSocket is an update channel, not the inbox of record.
                await websocket.send_json({"type": "heartbeat"})
    except WebSocketDisconnect:
        pass
    except Exception as exc:
        log.error("alert_stream_dependency_error", error_type=type(exc).__name__)
        await websocket.close(code=1011, reason="Alert stream unavailable")
    finally:
        await pubsub.unsubscribe(channel)
        await pubsub.aclose()

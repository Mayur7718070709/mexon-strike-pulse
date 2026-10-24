import asyncio
import json
import math
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from uuid import UUID

import structlog
from sqlalchemy import text

from app.dhan.errors import DhanError
from app.expired_options.coverage import ExpiredOptionsCoverageService
from app.expired_options.repository import ExpiredOptionsRepository

log = structlog.get_logger(__name__)
REQUIRED_DATA = ["open", "high", "low", "close", "iv", "volume", "strike", "oi", "spot"]


class ExpiredOptionsWorker:
    """Fetch actual Dhan rolling-option observations and persist restartably."""

    def __init__(self, provider, session_factory):
        self.provider = provider
        self.session_factory = session_factory

    async def run(self, job_id: UUID | str) -> dict:
        job_id = UUID(str(job_id))
        async with self.session_factory() as session:
            repo = ExpiredOptionsRepository(session)
            job = await repo.get_job(job_id)
            if job is None:
                return {"status": "missing"}
            if job["status"] == "succeeded":
                return {"status": "succeeded", "job_id": str(job_id)}
            claim = await session.execute(text("""
              update public.expired_options_ingestion_jobs set status='running',
                started_at=coalesce(started_at,now()),attempt_count=attempt_count+1,updated_at=now()
              where id=:id and status in ('queued','partial','failed') returning id
            """), {"id": job_id})
            if claim.scalar_one_or_none() is None:
                return {"job_id": str(job_id), "status": job["status"]}
            await session.commit()
            job = await repo.get_job(job_id)
            for chunk in [item for item in job["chunks"] if item["status"] != "succeeded"]:
                await self._process_chunk(session, job, chunk)
                job = await repo.get_job(job_id)
                if chunk["is_sample"] and not job["sample_verified"]:
                    break
            job = await repo.get_job(job_id)
            failed = sum(item["status"] in {"failed", "partial"} for item in job["chunks"])
            pending = sum(item["status"] in {"queued", "running"} for item in job["chunks"])
            status = "succeeded" if not failed and not pending and job["sample_verified"] else (
                "partial" if job["chunks_completed"] else "failed")
            report = await ExpiredOptionsCoverageService(session).for_job(job)
            await session.execute(text("""
              update public.expired_options_ingestion_jobs set status=:status,
                chunks_failed=:failed,completed_at=now(),coverage_report=cast(:report as jsonb),updated_at=now()
              where id=:id
            """), {"status": status, "failed": failed, "report": json.dumps(report, default=str), "id": job_id})
            await session.commit()
            return {"job_id": str(job_id), "status": status, "chunks_total": job["chunks_total"],
                    "chunks_completed": job["chunks_completed"], "chunks_failed": failed, "coverage": report}

    async def _process_chunk(self, session, job: dict, chunk: dict):
        repo = ExpiredOptionsRepository(session)
        metadata = job.get("metadata") or {}
        for attempt in range(2):
            await session.execute(text("""
              update public.expired_options_ingestion_chunks set status='running',
                attempt_count=attempt_count+1,started_at=coalesce(started_at,now()),
                error_code=null,error_message=null,updated_at=now() where id=:id
            """), {"id": chunk["id"]})
            await session.commit()
            try:
                response = await self.provider.get_expired_options(
                    exchange_segment="NSE_FNO", security_id=str(job["security_id"]),
                    instrument=job["instrument"], interval=job["interval"],
                    expiry_flag=job["expiry_flag"], expiry_code=job["expiry_code"],
                    strike=chunk["strike_selection"], option_type=chunk["option_type"],
                    required_data=REQUIRED_DATA, from_date=chunk["range_start"].date(),
                    to_date=chunk["range_end"].date())
                envelope = response.get("data", {})
                side_key = "ce" if chunk["option_type"] == "CALL" else "pe"
                side = envelope.get(side_key)
                rows, invalid = self._normalize(side, metadata.get("symbol", ""), chunk["option_type"])
                if chunk["is_sample"] and not rows:
                    raw_count = len(side.get("timestamp", [])) if isinstance(side, dict) else 0
                    no_data = raw_count == 0
                    await repo.mark_chunk(chunk["id"], status="partial", received=raw_count,
                        stored=0, invalid=invalid, first=None, last=None,
                        error_code="no_provider_observations" if no_data else "invalid_observations",
                        error_message="Dhan returned no observations for the initial sample selection" if no_data
                                      else "Dhan returned sample rows, but none passed validation")
                    await session.execute(text("""
                      update public.expired_options_ingestion_jobs set
                        chunks_completed=(select count(*) from public.expired_options_ingestion_chunks where job_id=:job and status in ('succeeded','partial')),
                        chunks_failed=(select count(*) from public.expired_options_ingestion_chunks where job_id=:job and status in ('failed','partial')),
                        rows_received=(select coalesce(sum(rows_received),0) from public.expired_options_ingestion_chunks where job_id=:job),
                        rows_upserted=(select coalesce(sum(rows_upserted),0) from public.expired_options_ingestion_chunks where job_id=:job),
                        updated_at=now() where id=:job
                    """), {"job": job["id"]})
                    await session.commit()
                    return
                upserted = await repo.upsert_observations(job=job, chunk=chunk, values=rows)
                received_timestamps = [value["provider_timestamp"] for value in rows]
                empty = not rows
                status = "partial" if invalid or empty else "succeeded"
                error_code = "invalid_observations" if invalid else ("no_provider_observations" if empty else None)
                error_message = ("Invalid provider rows were skipped after validation" if invalid else
                                 "Dhan returned no observations for the requested rolling selection" if empty else None)
                await repo.mark_chunk(chunk["id"], status=status, received=(side and len(side.get("timestamp", []))) or 0,
                    stored=upserted, invalid=invalid, first=min(received_timestamps) if received_timestamps else None,
                    last=max(received_timestamps) if received_timestamps else None,
                    error_code=error_code, error_message=error_message)
                await session.execute(text("""
                  update public.expired_options_ingestion_jobs set
                    sample_verified=sample_verified or :sample,
                    chunks_completed=(select count(*) from public.expired_options_ingestion_chunks where job_id=:job and status in ('succeeded','partial')),
                    chunks_failed=(select count(*) from public.expired_options_ingestion_chunks where job_id=:job and status in ('failed','partial')),
                    rows_received=(select coalesce(sum(rows_received),0) from public.expired_options_ingestion_chunks where job_id=:job),
                    rows_upserted=(select coalesce(sum(rows_upserted),0) from public.expired_options_ingestion_chunks where job_id=:job),
                    updated_at=now() where id=:job
                """), {"sample": chunk["is_sample"] and bool(rows), "job": job["id"]})
                await session.commit()
                return
            except Exception as exc:
                await session.rollback()
                if attempt == 0:
                    await asyncio.sleep(1)
                    continue
                message = self._safe_message(exc)
                log.error("expired_options_chunk_failed", job_id=str(job["id"]),
                          chunk_id=str(chunk["id"]), error_type=type(exc).__name__)
                await repo.mark_chunk(chunk["id"], status="failed", received=0, stored=0, invalid=0,
                    first=None, last=None, error_code=type(exc).__name__[:80], error_message=message)
                await session.execute(text("""
                  update public.expired_options_ingestion_jobs set error_code=:code,error_message=:message,updated_at=now()
                  where id=:id
                """), {"code": type(exc).__name__[:80], "message": message, "id": job["id"]})
                await session.commit()
                return

    @staticmethod
    def _normalize(side, symbol: str, option_type: str) -> tuple[list[dict], int]:
        if side is None:
            return [], 0
        if not isinstance(side, dict) or not isinstance(side.get("timestamp"), list):
            raise ValueError("Dhan rolling-option side is malformed")
        timestamps = side["timestamp"]
        fields = ("open", "high", "low", "close", "volume", "oi", "iv", "spot", "strike")
        if any(name in side and (not isinstance(side[name], list) or len(side[name]) != len(timestamps)) for name in fields):
            raise ValueError("Dhan rolling-option fields are not aligned with the timestamp series")
        rows, invalid = [], 0
        for idx, epoch in enumerate(timestamps):
            try:
                if isinstance(epoch, bool) or not isinstance(epoch, (int, float)) or not math.isfinite(epoch):
                    raise ValueError("invalid provider timestamp")
                provider_time = datetime.fromtimestamp(epoch, timezone.utc)
                values = {name: side[name][idx] if name in side else None for name in fields}
                strike = ExpiredOptionsWorker._decimal(values["strike"], positive=True, required=True)
                prices = {name: ExpiredOptionsWorker._decimal(values[name], positive=False, required=False)
                          for name in ("open", "high", "low", "close")}
                if any(value is None for value in prices.values()):
                    raise ValueError("OHLC field unavailable")
                if prices["high"] < max(prices["open"], prices["low"], prices["close"]) or prices["low"] > min(prices["open"], prices["high"], prices["close"]):
                    raise ValueError("invalid OHLC relationship")
                volume = ExpiredOptionsWorker._count(values["volume"])
                oi = ExpiredOptionsWorker._count(values["oi"])
                iv = ExpiredOptionsWorker._decimal(values["iv"], positive=False, required=False)
                spot = ExpiredOptionsWorker._decimal(values["spot"], positive=True, required=False)
                observation = {"source": "DHANHQ", "underlying": symbol, "timestamp": epoch,
                               "strike": str(strike), "option_type": option_type,
                               "open": str(prices["open"]),
                               "high": str(prices["high"]), "low": str(prices["low"]),
                               "close": str(prices["close"]), "volume": volume, "oi": oi,
                               "iv": str(iv) if iv is not None else None,
                               "spot": str(spot) if spot is not None else None}
                rows.append({"strike": strike, "option_type": option_type.lower(),
                             "provider_timestamp": provider_time, "ingested_at": datetime.now(timezone.utc),
                             "option_contract_id": None, "open_price": prices["open"],
                             "high_price": prices["high"], "low_price": prices["low"],
                             "close_price": prices["close"], "volume": volume, "open_interest": oi,
                             "iv": iv, "spot": spot, "observation": json.dumps(observation)})
            except (ValueError, TypeError, OverflowError, InvalidOperation):
                invalid += 1
        return rows, invalid

    @staticmethod
    def _decimal(value, *, positive: bool, required: bool) -> Decimal | None:
        if value is None:
            if required:
                raise ValueError("required provider value is missing")
            return None
        if isinstance(value, bool):
            raise ValueError("boolean is not a numeric provider value")
        try:
            normalized = Decimal(str(value))
        except (InvalidOperation, ValueError) as exc:
            raise ValueError("provider value is not numeric") from exc
        if not normalized.is_finite() or (positive and normalized <= 0) or (not positive and normalized < 0):
            raise ValueError("provider numeric value is out of range")
        return normalized

    @staticmethod
    def _count(value) -> int | None:
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError("provider count is invalid")
        return value

    @staticmethod
    def _safe_message(exc: Exception) -> str:
        if isinstance(exc, (DhanError, ValueError)):
            return str(exc)[:500]
        return "Expired-options processing failed due to a dependency or persistence error."

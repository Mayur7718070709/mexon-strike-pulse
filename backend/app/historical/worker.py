import asyncio
import json
from uuid import UUID

import structlog
from sqlalchemy import text

from app.dhan.errors import DhanError
from app.historical.coverage import HistoricalCoverageService
from app.historical.repository import HistoricalRepository
from app.historical.validation import HistoricalValidationService

log = structlog.get_logger(__name__)


class HistoricalIngestionWorker:
    """Serially ingest persisted chunks; successful chunks survive process restarts."""

    def __init__(self, provider, session_factory):
        self.provider = provider
        self.session_factory = session_factory
        self.validation = HistoricalValidationService()

    async def run(self, job_id: UUID | str) -> dict:
        job_id = UUID(str(job_id))
        async with self.session_factory() as session:
            repo = HistoricalRepository(session)
            job = await repo.get_job(job_id)
            if job is None:
                return {"status": "missing"}
            if job["status"] == "succeeded":
                return {"status": "succeeded", "job_id": str(job_id)}
            claimed = await session.execute(text("""
              update public.historical_ingestion_jobs set status='running',started_at=coalesce(started_at,now()),
                attempt_count=attempt_count+1,updated_at=now()
              where id=:id and status in ('queued','partial','failed') returning id
            """), {"id": job_id})
            if claimed.scalar_one_or_none() is None:
                return {"job_id": str(job_id), "status": job["status"]}
            await session.commit()
            metadata = job["metadata"] or {}
            interval = metadata["interval"]
            chunks = [chunk for chunk in job["chunks"] if chunk["status"] != "succeeded"]
            for chunk in chunks:
                await self._process_chunk(session, job, chunk, interval)
                # Refresh persisted job data for progress and current success/failure counts.
                job = await repo.get_job(job_id)
                if chunk["is_sample"] and not job["sample_verified"]:
                    # Never start bulk chunks unless the actual provider sample succeeded.
                    break
            job = await repo.get_job(job_id)
            failed = sum(1 for chunk in job["chunks"] if chunk["status"] in {"failed", "partial"})
            pending = sum(1 for chunk in job["chunks"] if chunk["status"] in {"queued", "running"})
            status = "succeeded" if failed == 0 and pending == 0 and job["sample_verified"] else ("partial" if job["chunks_completed"] else "failed")
            report = await HistoricalCoverageService(session).for_job(job)
            await session.execute(text("""
              update public.historical_ingestion_jobs set status=:status,chunks_failed=:failed,
                completed_at=now(),coverage_report=cast(:report as jsonb),updated_at=now()
              where id=:id
            """), {"status": status, "failed": failed, "report": json.dumps(report, default=str), "id": job_id})
            await session.commit()
            return {"job_id": str(job_id), "status": status, "chunks_total": job["chunks_total"],
                    "chunks_completed": job["chunks_completed"], "chunks_failed": failed,
                    "coverage_report": report}

    async def _process_chunk(self, session, job: dict, chunk: dict, interval: str):
        repo = HistoricalRepository(session)
        metadata = job["metadata"] or {}
        is_sample = chunk["is_sample"]
        start, end = chunk["range_start"].date(), chunk["range_end"].date()
        for attempt in range(2):
            await session.execute(text("""
              update public.historical_ingestion_chunks set status='running',attempt_count=attempt_count+1,
                     started_at=coalesce(started_at,now()),error_code=null,error_message=null,updated_at=now()
              where id=:id
            """), {"id": chunk["id"]})
            await session.commit()
            try:
                response = await self.provider.get_historical_candles(
                    security_id=metadata["security_id"], exchange_segment=metadata["exchange_segment"],
                    instrument=metadata["instrument_type"], from_date=start, to_date=end,
                    interval=None if interval == "1d" else interval,
                    oi=metadata["instrument_type"] in {"OPTIDX", "OPTSTK", "FUTIDX", "FUTSTK"},
                )
                data = response.get("data", {})
                normalized = self.validation.normalize(data)
                if is_sample and not normalized["candles"]:
                    raise ValueError("Dhan returned no candles for the initial seven-day sample range")
                if normalized["invalid"]:
                    log.warning("historical_invalid_candles", job_id=str(job["id"]), chunk_id=str(chunk["id"]), count=normalized["invalid"])
                existing = await repo.existing_timestamps(provider_id=job["provider_id"], instrument_id=job["instrument_id"],
                                                          timeframe=job["timeframe"], candles=normalized["candles"])
                repeated = normalized["duplicates"] + sum(1 for candle in normalized["candles"] if candle["timestamp"] in existing)
                inserted_count = await repo.upsert_candles(provider_id=job["provider_id"], instrument_id=job["instrument_id"],
                                                            timeframe=job["timeframe"], candles=normalized["candles"])
                received_timestamps = [row["timestamp"] for row in normalized["candles"]]
                empty_range = not normalized["candles"]
                chunk_status = "partial" if normalized["invalid"] or empty_range else "succeeded"
                await session.execute(text("""
                  update public.historical_ingestion_chunks set status=:status,rows_received=:received,
                    rows_upserted=:upserted,invalid_rows=:invalid,duplicate_rows=:duplicates,
                    provider_first_timestamp=:first,provider_last_timestamp=:last,
                    error_code=case when :invalid > 0 then 'invalid_candles' when :empty then 'no_provider_observations' else null end,
                    error_message=case when :invalid > 0 then 'Invalid provider rows were skipped after validation'
                      when :empty then 'Dhan returned no observations for this requested range' else null end,
                    completed_at=now(),updated_at=now()
                  where id=:id
                """), {"status": chunk_status, "received": normalized["received"], "upserted": inserted_count,
                       "invalid": normalized["invalid"], "duplicates": repeated,
                       "empty": empty_range,
                       "first": min(received_timestamps) if received_timestamps else None,
                       "last": max(received_timestamps) if received_timestamps else None, "id": chunk["id"]})
                # Rows upserted means accepted provider rows; DB uniqueness makes retry idempotent.
                range_json = json.dumps({"from": start.isoformat(), "to_exclusive": end.isoformat(), "observations": len(normalized["candles"])})
                await session.execute(text("""
                  update public.historical_ingestion_jobs set sample_verified=sample_verified or :sample,
                    chunks_completed=(select count(*) from public.historical_ingestion_chunks where job_id=:job and status in ('succeeded','partial')),
                    rows_received=(select coalesce(sum(rows_received),0) from public.historical_ingestion_chunks where job_id=:job),
                    rows_upserted=(select coalesce(sum(rows_upserted),0) from public.historical_ingestion_chunks where job_id=:job),
                    successful_ranges=case when not :has_data or successful_ranges @> cast(:range as jsonb) then successful_ranges
                      else successful_ranges || cast(:range as jsonb) end,updated_at=now() where id=:job
                """), {"sample": is_sample and bool(normalized["candles"]), "has_data": bool(normalized["candles"]),
                       "range": range_json, "job": job["id"]})
                await session.commit()
                return
            except Exception as exc:
                await session.rollback()
                if attempt == 0:
                    await asyncio.sleep(1)
                    continue
                log.error("historical_chunk_failed", job_id=str(job["id"]), chunk_id=str(chunk["id"]), error_type=type(exc).__name__)
                await session.execute(text("""
                  update public.historical_ingestion_chunks set status='failed',error_code=:code,
                    error_message=:message,completed_at=now(),updated_at=now() where id=:id
                """), {"code": type(exc).__name__[:80], "message": self._safe_error_message(exc), "id": chunk["id"]})
                await session.execute(text("update public.historical_ingestion_jobs set error_code=:code,error_message=:message,updated_at=now() where id=:job"),
                                      {"code": type(exc).__name__[:80], "message": self._safe_error_message(exc), "job": job["id"]})
                await session.commit()
                return

    @staticmethod
    def _safe_error_message(exc: Exception) -> str:
        if isinstance(exc, DhanError):
            return str(exc)[:500]
        if isinstance(exc, ValueError):
            return str(exc)[:500]
        return "Historical processing failed due to a dependency or persistence error."


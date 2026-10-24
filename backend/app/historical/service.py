import hashlib
import json
from datetime import date, datetime, timedelta
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import text

from app.historical.repository import HistoricalRepository, TIMEFRAMES


class HistoricalDataService:
    """Validate, resolve and enqueue a Dhan-confirmed historical backfill."""

    def __init__(self, session):
        self.repository = HistoricalRepository(session)
        self.session = session

    async def submit(self, *, user_id: UUID, underlying: str | None, instrument: str | None,
                     interval: str, from_date: date, to_date: date, redis_url: str) -> dict:
        resolved = await self.repository.resolve_instrument(underlying=underlying, instrument=instrument)
        if resolved is None:
            raise ValueError("No active Dhan instrument-master mapping matches the requested underlying or instrument")
        if interval != "1d" and not resolved["is_active"]:
            raise ValueError("Dhan documents intraday history for active instruments; this provider mapping is inactive")
        if resolved["exchange_segment"] not in {"IDX_I", "NSE_EQ", "NSE_FNO"}:
            raise ValueError("The selected instrument segment is not supported for this historical backfill")
        if resolved["instrument_type"] not in {"INDEX", "EQUITY", "OPTIDX", "OPTSTK", "FUTIDX", "FUTSTK"}:
            raise ValueError("The selected Dhan instrument type is not supported for this historical backfill")
        if interval not in {"1", "5", "15", "25", "60", "1d"}:
            raise ValueError("interval must be one of Dhan's documented values: 1, 5, 15, 25, 60, or 1d")
        if from_date >= to_date:
            raise ValueError("from_date must be earlier than to_date; to_date is exclusive per Dhan v2")
        today = datetime.now(ZoneInfo("Asia/Kolkata")).date()
        if from_date > today or to_date > today + timedelta(days=1):
            raise ValueError("Historical backfills cannot include future dates")

        max_days = 90 if interval != "1d" else 365
        sample_start = max(from_date, to_date - timedelta(days=7))
        chunks = [(sample_start, to_date, True)]
        cursor = from_date
        while cursor < sample_start:
            next_date = min(to_date, cursor + timedelta(days=max_days))
            next_date = min(next_date, sample_start)
            chunks.append((cursor, next_date, False))
            cursor = next_date

        request_identity = {"instrument_id": str(resolved["instrument_id"]), "interval": interval,
                            "from_date": from_date.isoformat(), "to_date": to_date.isoformat()}
        key = hashlib.sha256(json.dumps(request_identity, sort_keys=True).encode()).hexdigest()
        row = await self.repository.create_job(user_id=user_id, instrument=dict(resolved), interval=interval,
                                               start=from_date, end=to_date, key=key, chunks=chunks)
        job_id = row["id"]
        response_status = row["status"]
        if row["status"] in {"queued", "failed", "partial"}:
            response_status = "queued"
            await self.session.execute(text("""
              update public.historical_ingestion_jobs set status='queued', error_code=null, error_message=null,
                     completed_at=null, available_at=now(), updated_at=now() where id=:id
            """), {"id": job_id})
            await self.session.commit()
            try:
                from arq import create_pool
                from arq.connections import RedisSettings
                pool = await create_pool(RedisSettings.from_dsn(redis_url))
                try:
                    await pool.enqueue_job("run_historical_ingestion", str(job_id),
                                           _job_id=f"historical:{job_id}:{row['attempt_count']}")
                finally:
                    await pool.aclose()
            except Exception:
                await self.session.execute(text("""
                  update public.historical_ingestion_jobs set status='failed', error_code='queue_unavailable',
                         error_message='Historical worker queue is unavailable', completed_at=now(), updated_at=now()
                  where id=:id
                """), {"id": job_id})
                await self.session.commit()
                raise RuntimeError("Historical worker queue is unavailable; job was recorded but not started")
        return {"job_id": str(job_id), "status": response_status, "source": "DHANHQ",
                "instrument": {"symbol": resolved["symbol"], "security_id": resolved["security_id"], "exchange_segment": resolved["exchange_segment"], "instrument_type": resolved["instrument_type"]},
                "interval": interval, "timeframe": TIMEFRAMES[interval], "from_date": from_date,
                "to_date_exclusive": to_date, "chunks_total": row["chunks_total"],
                "message": "A real Dhan sample request is the first worker step. No coverage is claimed before provider records are stored."}

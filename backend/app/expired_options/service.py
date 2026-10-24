import hashlib
import json
import calendar
from datetime import date, datetime, timedelta
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import text

from app.expired_options.repository import ExpiredOptionsRepository


class ExpiredOptionsService:
    """Create and queue Dhan expired-options backfills without assuming coverage."""

    def __init__(self, session):
        self.session = session
        self.repository = ExpiredOptionsRepository(session)

    async def submit(self, *, user_id: UUID, underlying: str, interval: str,
                     expiry_flag: str, expiry_code: int, strikes: list[str],
                     option_types: list[str], from_date: date, to_date: date,
                     redis_url: str) -> dict:
        symbol = underlying.strip().upper()
        resolved = await self.repository.resolve_underlying(symbol)
        if resolved is None:
            return {"status": "UNSUPPORTED", "available": False, "source": "DHANHQ",
                    "underlying": symbol, "reason": "No active NSE index or equity mapping from the Dhan instrument master supports this underlying."}
        if resolved["exchange_segment"] not in {"IDX_I", "NSE_EQ"}:
            raise ValueError("Expired options are supported only for NSE index/equity underlyings confirmed by the Dhan instrument master")
        if interval not in {"1", "5", "15", "25", "60"}:
            raise ValueError("interval must be one of Dhan's documented minute intervals: 1, 5, 15, 25, 60")
        if expiry_flag not in {"WEEK", "MONTH"} or expiry_code not in {0, 1, 2}:
            raise ValueError("Use Dhan's documented expiryFlag WEEK or MONTH and expiryCode 0, 1, or 2")
        if from_date >= to_date:
            raise ValueError("from_date must be earlier than to_date; Dhan's toDate is exclusive")
        today = datetime.now(ZoneInfo("Asia/Kolkata")).date()
        first_supported_day = date(today.year - 5, today.month,
                                   min(today.day, calendar.monthrange(today.year - 5, today.month)[1]))
        if from_date < first_supported_day or to_date > today:
            raise ValueError("Dhan rolling expired-options requests are limited to the documented last five years and cannot include future dates")
        if not strikes or len(set(strikes)) != len(strikes) or len(strikes) > 21:
            raise ValueError("Provide 1 to 21 distinct documented ATM-relative strike selections")
        # Dhan's annexure identifies expiryCode 0 as current/near expiry; the
        # expired-options page allows index options ATM±10 near expiry. For all
        # other cases, apply the documented ATM±3 limit.
        max_offset = 10 if resolved["instrument_type"] == "INDEX" and expiry_code == 0 else 3
        for strike in strikes:
            if strike != "ATM":
                import re
                match = re.fullmatch(r"ATM([+-])(\d+)", strike)
                if not match or not 1 <= int(match.group(2)) <= max_offset:
                    raise ValueError(f"{symbol} supports ATM-relative selections through ATM±{max_offset} under the documented instrument rules")
        if not option_types or len(set(option_types)) != len(option_types) or any(value not in {"CALL", "PUT"} for value in option_types):
            raise ValueError("option_types must contain distinct CALL and/or PUT values")
        # One real 7-calendar-day provider sample (or the whole shorter range) precedes all bulk requests.
        sample_start = max(from_date, to_date - timedelta(days=7))
        chunks = []
        first_strike, first_type = strikes[0], option_types[0]
        chunks.append({"start": sample_start, "end": to_date, "strike": first_strike,
                       "option_type": first_type, "sample": True})
        # Keep all remaining provider calls <= 30 calendar days, the endpoint's documented chunk cap.
        for strike in strikes:
            for option_type in option_types:
                if strike == first_strike and option_type == first_type:
                    cursor = from_date
                    while cursor < sample_start:
                        end = min(sample_start, cursor + timedelta(days=30))
                        chunks.append({"start": cursor, "end": end, "strike": strike, "option_type": option_type, "sample": False})
                        cursor = end
                else:
                    cursor = from_date
                    while cursor < to_date:
                        end = min(to_date, cursor + timedelta(days=30))
                        chunks.append({"start": cursor, "end": end, "strike": strike, "option_type": option_type, "sample": False})
                        cursor = end

        identity = {"underlying_id": str(resolved["underlying_id"]), "interval": interval,
                    "expiry_flag": expiry_flag, "expiry_code": expiry_code, "strikes": strikes,
                    "option_types": option_types, "from": from_date.isoformat(), "to": to_date.isoformat()}
        key = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
        row = await self.repository.create_job(user_id=user_id, underlying=dict(resolved), interval=interval,
            expiry_flag=expiry_flag, expiry_code=expiry_code, strikes=strikes, option_types=option_types,
            start=from_date, end=to_date, chunks=chunks, key=key)
        response_status = row["status"]
        if row["status"] in {"queued", "failed", "partial"}:
            response_status = "queued"
            await self.session.execute(text("""
              update public.expired_options_ingestion_jobs set status='queued',error_code=null,
                error_message=null,completed_at=null,updated_at=now() where id=:id
            """), {"id": row["id"]})
            await self.session.commit()
            try:
                from arq import create_pool
                from arq.connections import RedisSettings
                pool = await create_pool(RedisSettings.from_dsn(redis_url))
                try:
                    await pool.enqueue_job("run_expired_options_ingestion", str(row["id"]),
                                           _job_id=f"expired-options:{row['id']}:{row['attempt_count']}")
                finally:
                    await pool.aclose()
            except Exception as exc:
                await self.session.execute(text("""
                  update public.expired_options_ingestion_jobs set status='failed',error_code='queue_unavailable',
                    error_message='Expired-options worker queue is unavailable',completed_at=now(),updated_at=now()
                  where id=:id
                """), {"id": row["id"]})
                await self.session.commit()
                raise RuntimeError("Expired-options worker queue is unavailable; the job was recorded but not started") from exc
        return {"job_id": str(row["id"]), "status": response_status, "source": "DHANHQ",
                "underlying": symbol, "underlying_security_id": resolved["security_id"],
                "exchange_segment": "NSE_FNO", "instrument": "OPTIDX" if resolved["instrument_type"] == "INDEX" else "OPTSTK",
                "interval": interval, "expiry_selector": {"expiry_flag": expiry_flag, "expiry_code": expiry_code},
                "calendar_expiry_date": None, "from_date": from_date, "to_date_exclusive": to_date,
                "chunks_total": row["chunks_total"],
                "message": "A small live Dhan historical query is the first worker step. Calendar expiry dates are not returned by this rolling endpoint and are not inferred."}

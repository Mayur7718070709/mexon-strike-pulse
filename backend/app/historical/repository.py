import json
from datetime import date, datetime, time, timezone
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


TIMEFRAMES = {"1": "1m", "5": "5m", "15": "15m", "25": "25m", "60": "60m", "1d": "1d"}


class HistoricalRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def resolve_instrument(self, *, underlying: str | None, instrument: str | None):
        if underlying:
            sql = """
              select i.id instrument_id, i.provider_id, i.security_id, i.exchange_segment,
                     i.instrument_type, i.symbol, i.display_name, i.underlying_symbol, i.is_active
              from public.underlying_assets u join public.instruments i on i.id=u.instrument_id
              join public.market_data_providers p on p.id=i.provider_id
              where p.code='DHANHQ' and u.root_symbol=:symbol and i.is_active
              order by case when i.exchange_segment='IDX_I' then 0 when i.exchange_segment='NSE_EQ' then 1 else 2 end
              limit 1
            """
            params = {"symbol": underlying.strip().upper()}
        else:
            sql = """
              select i.id instrument_id, i.provider_id, i.security_id, i.exchange_segment,
                     i.instrument_type, i.symbol, i.display_name, i.underlying_symbol, i.is_active
              from public.instruments i join public.market_data_providers p on p.id=i.provider_id
              where p.code='DHANHQ' and (upper(i.symbol)=:symbol or upper(i.display_name)=:symbol)
              order by i.expiry_date nulls first limit 1
            """
            params = {"symbol": instrument.strip().upper()}
        result = await self.session.execute(text(sql), params)
        return result.mappings().one_or_none()

    async def create_job(self, *, user_id: UUID, instrument: dict, interval: str, start: date, end: date, key: str, chunks: list[tuple[date, date, bool]]):
        timeframe = TIMEFRAMES[interval]
        result = await self.session.execute(text("""
          insert into public.historical_ingestion_jobs
            (requested_by,provider_id,instrument_id,timeframe,range_start,range_end,idempotency_key,status,chunks_total,metadata)
          values (:user_id,:provider_id,:instrument_id,:timeframe,:range_start,:range_end,:key,'queued',:chunk_count,
                  cast(:metadata as jsonb))
          on conflict (idempotency_key) do update set updated_at=now()
          returning id,status,chunks_total,chunks_completed,chunks_failed,sample_verified,attempt_count
        """), {"user_id": user_id, "provider_id": instrument["provider_id"], "instrument_id": instrument["instrument_id"],
               "timeframe": timeframe, "range_start": datetime.combine(start, time.min, timezone.utc),
               "range_end": datetime.combine(end, time.min, timezone.utc), "key": key, "chunk_count": len(chunks),
               "metadata": json.dumps({"source": "DHANHQ", "interval": interval, "symbol": instrument["symbol"], "exchange_segment": instrument["exchange_segment"], "security_id": instrument["security_id"]})})
        row = result.mappings().one()
        if row["status"] == "queued" and row["chunks_total"] == len(chunks):
            for chunk_start, chunk_end, is_sample in chunks:
                await self.session.execute(text("""
                  insert into public.historical_ingestion_chunks(job_id,range_start,range_end,is_sample)
                  values (:job_id,:start,:end,:is_sample) on conflict (job_id,range_start,range_end) do nothing
                """), {"job_id": row["id"], "start": datetime.combine(chunk_start, time.min, timezone.utc),
                       "end": datetime.combine(chunk_end, time.min, timezone.utc), "is_sample": is_sample})
        await self.session.commit()
        return row

    async def get_job(self, job_id: UUID):
        job = await self.session.execute(text("select * from public.historical_ingestion_jobs where id=:id"), {"id": job_id})
        row = job.mappings().one_or_none()
        if row is None:
            return None
        chunks = await self.session.execute(text("select * from public.historical_ingestion_chunks where job_id=:id order by is_sample desc,range_start"), {"id": job_id})
        result = dict(row)
        result["chunks"] = [dict(c) for c in chunks.mappings()]
        return result

    async def existing_timestamps(self, *, provider_id: UUID, instrument_id: UUID, timeframe: str, candles: list[dict]) -> set[datetime]:
        if not candles:
            return set()
        rows = await self.session.execute(text("""
          select candle_timestamp from public.historical_candles
          where provider_id=:provider and instrument_id=:instrument and timeframe=:timeframe
            and candle_timestamp >= :start and candle_timestamp <= :end
        """), {"provider": provider_id, "instrument": instrument_id, "timeframe": timeframe,
               "start": candles[0]["timestamp"], "end": candles[-1]["timestamp"]})
        return {row[0] for row in rows}

    async def upsert_candles(self, *, provider_id: UUID, instrument_id: UUID, timeframe: str, candles: list[dict]):
        if not candles:
            return 0
        sql = text("""
          insert into public.historical_candles
            (provider_id,instrument_id,timeframe,candle_timestamp,provider_timestamp,ingested_at,
             open_price,high_price,low_price,close_price,volume,open_interest)
          values (:provider_id,:instrument_id,:timeframe,:timestamp,:provider_timestamp,:ingested_at,
                  :open,:high,:low,:close,:volume,:open_interest)
          on conflict (provider_id,instrument_id,timeframe,candle_timestamp) do update set
            provider_timestamp=excluded.provider_timestamp, ingested_at=excluded.ingested_at,
            open_price=excluded.open_price, high_price=excluded.high_price, low_price=excluded.low_price,
            close_price=excluded.close_price, volume=excluded.volume, open_interest=excluded.open_interest, updated_at=now()
        """)
        await self.session.execute(sql, [{**candle, "provider_id": provider_id, "instrument_id": instrument_id, "timeframe": timeframe} for candle in candles])
        return len(candles)

    async def upsert_coverage_report(self, job_id: UUID, report: dict):
        await self.session.execute(text("update public.historical_ingestion_jobs set coverage_report=cast(:report as jsonb), updated_at=now() where id=:id"), {"id": job_id, "report": json.dumps(report, default=str)})
        await self.session.commit()

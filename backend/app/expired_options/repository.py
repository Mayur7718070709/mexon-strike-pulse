import json
from datetime import date, datetime, time, timezone
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


class ExpiredOptionsRepository:
    """Persistence for provider-confirmed rolling options observations and jobs."""

    def __init__(self, session: AsyncSession):
        self.session = session

    async def resolve_underlying(self, symbol: str):
        result = await self.session.execute(text("""
          select u.id underlying_id, u.root_symbol, i.id instrument_id, i.provider_id,
                 i.security_id, i.exchange_segment, i.instrument_type, i.symbol,
                 i.is_active, u.underlying_type
          from public.underlying_assets u join public.instruments i on i.id=u.instrument_id
          join public.market_data_providers p on p.id=i.provider_id
          where p.code='DHANHQ' and upper(u.root_symbol)=:symbol
            and i.is_active and i.exchange_segment in ('IDX_I','NSE_EQ')
            and i.instrument_type in ('INDEX','EQUITY')
          order by case when i.instrument_type='INDEX' then 0 else 1 end
          limit 1
        """), {"symbol": symbol.strip().upper()})
        return result.mappings().one_or_none()

    async def create_job(self, *, user_id: UUID, underlying: dict, interval: str,
                         expiry_flag: str, expiry_code: int, strikes: list[str],
                         option_types: list[str], start: date, end: date, chunks: list[dict], key: str):
        result = await self.session.execute(text("""
          insert into public.expired_options_ingestion_jobs
            (requested_by,provider_id,underlying_id,instrument_id,exchange_segment,security_id,
             instrument,interval,expiry_flag,expiry_code,strike_selections,option_types,
             range_start,range_end,idempotency_key,status,chunks_total,metadata)
          values (:user_id,:provider_id,:underlying_id,:instrument_id,'NSE_FNO',:security_id,
             :instrument,:interval,:expiry_flag,:expiry_code,cast(:strikes as jsonb),cast(:types as jsonb),
             :range_start,:range_end,:key,'queued',:chunk_count,cast(:metadata as jsonb))
          on conflict (requested_by,idempotency_key) do update set updated_at=now()
          returning id,status,chunks_total,chunks_completed,chunks_failed,sample_verified,attempt_count
        """), {
            "user_id": user_id, "provider_id": underlying["provider_id"],
            "underlying_id": underlying["underlying_id"], "instrument_id": underlying["instrument_id"],
            "security_id": underlying["security_id"],
            "instrument": "OPTIDX" if underlying["instrument_type"] == "INDEX" else "OPTSTK",
            "interval": interval, "expiry_flag": expiry_flag, "expiry_code": expiry_code,
            "strikes": json.dumps(strikes), "types": json.dumps(option_types),
            "range_start": datetime.combine(start, time.min, timezone.utc),
            "range_end": datetime.combine(end, time.min, timezone.utc), "key": key,
            "chunk_count": len(chunks),
            "metadata": json.dumps({"source": "DHANHQ", "symbol": underlying["root_symbol"],
                                    "underlying_segment": underlying["exchange_segment"],
                                    "instrument_type": underlying["instrument_type"]}),
        })
        row = result.mappings().one()
        if row["status"] == "queued":
            for chunk in chunks:
                await self.session.execute(text("""
                  insert into public.expired_options_ingestion_chunks
                    (job_id,range_start,range_end,strike_selection,option_type,is_sample)
                  values (:job,:start,:end,:strike,:option_type,:sample)
                  on conflict (job_id,range_start,range_end,strike_selection,option_type) do nothing
                """), {"job": row["id"], "start": datetime.combine(chunk["start"], time.min, timezone.utc),
                       "end": datetime.combine(chunk["end"], time.min, timezone.utc),
                       "strike": chunk["strike"], "option_type": chunk["option_type"],
                       "sample": chunk["sample"]})
        await self.session.commit()
        return row

    async def get_job(self, job_id: UUID):
        result = await self.session.execute(text("select * from public.expired_options_ingestion_jobs where id=:id"), {"id": job_id})
        job = result.mappings().one_or_none()
        if job is None:
            return None
        chunks = await self.session.execute(text("""
          select * from public.expired_options_ingestion_chunks where job_id=:id
          order by is_sample desc,range_start,strike_selection,option_type
        """), {"id": job_id})
        value = dict(job)
        value["chunks"] = [dict(chunk) for chunk in chunks.mappings()]
        return value

    async def upsert_observations(self, *, job: dict, chunk: dict, values: list[dict]) -> int:
        if not values:
            return 0
        sql = text("""
          insert into public.expired_option_observations
            (provider_id,underlying_id,option_expiry_id,option_contract_id,strike,option_type,
             observation_kind,provider_timestamp,ingested_at,last_price,volume,open_interest,
             implied_volatility,observation,expiry_flag,expiry_code,strike_selection,interval,
             source,underlying_security_id,exchange_segment,open_price,high_price,low_price,close_price,spot_price)
          values (:provider_id,:underlying_id,null,:option_contract_id,:strike,:option_type,
             :observation_kind,:provider_timestamp,:ingested_at,null,:volume,:open_interest,
             :iv,cast(:observation as jsonb),:expiry_flag,:expiry_code,:strike_selection,:interval,
             'DHANHQ',:security_id,'NSE_FNO',:open_price,:high_price,:low_price,:close_price,:spot)
          on conflict (provider_id,underlying_id,observation_kind,strike,option_type,provider_timestamp)
          where source='DHANHQ' and expiry_flag is not null and expiry_code is not null
            and strike_selection is not null and interval is not null
          do update set option_contract_id=excluded.option_contract_id, ingested_at=excluded.ingested_at,
             last_price=excluded.last_price, volume=excluded.volume, open_interest=excluded.open_interest,
             implied_volatility=excluded.implied_volatility, observation=excluded.observation,
             open_price=excluded.open_price, high_price=excluded.high_price, low_price=excluded.low_price,
             close_price=excluded.close_price, spot_price=excluded.spot_price, updated_at=now()
        """)
        await self.session.execute(sql, [{**item, "provider_id": job["provider_id"],
            "underlying_id": job["underlying_id"], "expiry_flag": job["expiry_flag"],
            "expiry_code": job["expiry_code"], "strike_selection": chunk["strike_selection"],
            "interval": job["interval"], "security_id": str(job["security_id"]),
            "observation_kind": self.observation_kind(job, chunk)}
            for item in values])
        return len(values)

    async def observations_for_job(self, job: dict):
        result = await self.session.execute(text("""
          select strike_selection,option_type,strike,provider_timestamp,open_price,high_price,low_price,close_price,
                 volume,open_interest,implied_volatility,spot_price,source,expiry_flag,expiry_code,interval
          from public.expired_option_observations
          where provider_id=:provider_id and underlying_id=:underlying_id
            and expiry_flag=:expiry_flag and expiry_code=:expiry_code and interval=:interval
            and provider_timestamp >= :start and provider_timestamp < :end
            and strike_selection = any(cast(:strikes as text[]))
            and option_type = any(cast(:types as text[]))
          order by strike_selection,option_type,strike,provider_timestamp
        """), {"provider_id": job["provider_id"], "underlying_id": job["underlying_id"],
               "expiry_flag": job["expiry_flag"], "expiry_code": job["expiry_code"],
               "interval": job["interval"], "start": job["range_start"], "end": job["range_end"],
               "strikes": list(job["strike_selections"]),
               "types": [value.lower() for value in job["option_types"]]})
        return [dict(row) for row in result.mappings()]

    @staticmethod
    def observation_kind(job: dict, chunk: dict) -> str:
        return f"dhan_rollingoption:{job['interval']}:{job['expiry_flag']}:{job['expiry_code']}:{chunk['strike_selection']}"

    async def mark_chunk(self, chunk_id: UUID, *, status: str, received: int, stored: int,
                         invalid: int, first: datetime | None, last: datetime | None,
                         error_code: str | None = None, error_message: str | None = None):
        await self.session.execute(text("""
          update public.expired_options_ingestion_chunks set status=:status,rows_received=:received,
            rows_upserted=:stored,invalid_rows=:invalid,provider_first_timestamp=:first,
            provider_last_timestamp=:last,error_code=:error_code,error_message=:error_message,
            completed_at=now(),updated_at=now() where id=:id
        """), {"status": status, "received": received, "stored": stored, "invalid": invalid,
               "first": first, "last": last, "error_code": error_code,
               "error_message": error_message, "id": chunk_id})
        await self.session.commit()


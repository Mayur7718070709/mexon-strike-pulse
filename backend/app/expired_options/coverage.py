from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


class ExpiredOptionsCoverageService:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def report(self, underlying_id, provider_id, symbol: str, interval: str,
                     expiry_flag: str, expiry_code: int, strike_selection: str | None = None,
                     option_type: str | None = None, start=None, end=None) -> dict:
        normalized_type = option_type.lower() if option_type else None
        conditions = """
            underlying_id=:underlying_id and provider_id=:provider_id
            and interval=:interval and expiry_flag=:expiry_flag and expiry_code=:expiry_code
            and (:strike_selection is null or strike_selection=:strike_selection)
            and (:option_type is null or option_type=:option_type)
            and (:start is null or provider_timestamp >= :start)
            and (:end is null or provider_timestamp < :end)
        """
        params = {"underlying_id": underlying_id, "provider_id": provider_id,
                  "interval": interval, "expiry_flag": expiry_flag, "expiry_code": expiry_code,
                  "strike_selection": strike_selection, "option_type": normalized_type,
                  "start": start, "end": end}
        summary = await self.session.execute(text(f"""
          select min(provider_timestamp) earliest,max(provider_timestamp) latest,count(*) observations
          from public.expired_option_observations
          where {conditions}
        """), params)
        values = summary.mappings().one()
        gaps_result = await self.session.execute(text(f"""
          with filtered as (
            select distinct provider_timestamp from public.expired_option_observations where {conditions}
          ), ordered as (
            select provider_timestamp current_time,
                   lag(provider_timestamp) over (order by provider_timestamp) previous_time
            from filtered
          ), gaps as (
            select previous_time,current_time,
                   greatest(1,floor(extract(epoch from (current_time-previous_time))/:interval_seconds)-1)::bigint missing_count
            from ordered
            where previous_time is not null
              and timezone('UTC',previous_time)::date=timezone('UTC',current_time)::date
              and extract(epoch from (current_time-previous_time)) > :interval_seconds * 1.5
          )
          select previous_time,current_time,missing_count,count(*) over() total
          from gaps order by previous_time limit 1000
        """), {**params, "interval_seconds": int(interval) * 60})
        gap_rows = [dict(row) for row in gaps_result.mappings()]
        earliest, latest, observations = values["earliest"], values["latest"], values["observations"]
        gaps = [{"from": row["previous_time"], "to": row["current_time"],
                 "missing_observations_estimate": row["missing_count"]} for row in gap_rows]
        gap_count = gap_rows[0]["total"] if gap_rows else 0
        return {
            "status": "AVAILABLE" if observations else "NO OBSERVATIONS",
            "source": "DHANHQ", "symbol": symbol, "interval": interval,
            "expiry_selector": {"expiry_flag": expiry_flag, "expiry_code": expiry_code},
            "strike_selection": strike_selection, "option_type": option_type.upper() if option_type else None,
            "earliest_available": earliest, "latest_available": latest,
            "observations": observations, "missing_data_ranges": gaps,
            "missing_data_ranges_total": gap_count,
            "missing_data_ranges_truncated": gap_count > len(gaps), "coverage_is_provider_observed": True,
            "requested_range": {"from": start, "to_exclusive": end} if start is not None or end is not None else None,
            "calendar_expiry_date": None,
            "note": "Dhan rolling-option response provides a relative expiry selector, not a calendar expiry date. A null range means no observations were stored for this selection.",
        }

    async def for_job(self, job: dict) -> dict:
        metadata = job.get("metadata") or {}
        return await self.report(job["underlying_id"], job["provider_id"], metadata.get("symbol", "unknown"),
                                 job["interval"], job["expiry_flag"], job["expiry_code"],
                                 start=job["range_start"], end=job["range_end"])

from datetime import datetime, timedelta

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.historical.repository import HistoricalRepository, TIMEFRAMES


class HistoricalCoverageService:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def report(self, instrument_id, provider_id, symbol: str, interval: str, requested_start=None, requested_end=None) -> dict:
        timeframe = TIMEFRAMES[interval]
        rows = await self.session.execute(text("""
          select candle_timestamp from public.historical_candles
          where instrument_id=:instrument and provider_id=:provider and timeframe=:timeframe
          order by candle_timestamp
        """), {"instrument": instrument_id, "provider": provider_id, "timeframe": timeframe})
        timestamps = [row[0] for row in rows]
        gaps, gap_count = self._gaps(timestamps, interval)
        return {
            "source": "DHANHQ", "symbol": symbol, "interval": interval, "timeframe": timeframe,
            "earliest_available": timestamps[0] if timestamps else None,
            "latest_available": timestamps[-1] if timestamps else None,
            "observations": len(timestamps), "missing_data_ranges": gaps,
            "missing_data_ranges_total": gap_count, "missing_data_ranges_truncated": gap_count > len(gaps),
            "gap_detection": "Gaps between stored provider timestamps; weekends/overnight session breaks are excluded. Exchange holidays are not modeled.",
            "requested_range": {"from": requested_start, "to_exclusive": requested_end} if requested_start or requested_end else None,
            "coverage_is_provider_observed": True,
        }

    @staticmethod
    def _gaps(timestamps: list[datetime], interval: str) -> tuple[list[dict], int]:
        gaps = []
        intraday_minutes = int(interval) if interval != "1d" else None
        for previous, current in zip(timestamps, timestamps[1:]):
            delta = current - previous
            if intraday_minutes:
                # Session breaks and overnight gaps are not missing candles.
                if previous.date() != current.date() or delta <= timedelta(minutes=intraday_minutes * 1.5):
                    continue
                count = max(1, int(delta.total_seconds() // (intraday_minutes * 60)) - 1)
            else:
                days = (current.date() - previous.date()).days
                expected = sum(1 for offset in range(1, days) if (previous + timedelta(days=offset)).weekday() < 5)
                count = expected
                if count == 0:
                    continue
            gaps.append({"from": previous, "to": current, "missing_observations_estimate": count})
        return gaps[:1000], len(gaps)

    async def for_job(self, job: dict) -> dict:
        metadata = job.get("metadata") or {}
        return await self.report(job["instrument_id"], job["provider_id"], metadata.get("symbol", "unknown"), metadata.get("interval", "1d"), job["range_start"], job["range_end"])

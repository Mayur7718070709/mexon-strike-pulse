import json
from datetime import date
from decimal import Decimal

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


class ScannerRepository:
    """Loads only Dhan-confirmed contract mappings and persisted source observations."""

    def __init__(self, session: AsyncSession):
        self.session = session

    async def scan_contracts(self, underlying: str, expiry: date) -> dict | None:
        result = await self.session.execute(text("""
          select u.id underlying_id,u.root_symbol,ui.id underlying_instrument_id,
                 ui.security_id underlying_security_id,ui.exchange_segment underlying_segment,
                 e.id expiry_id,e.expiry_date,
                 c.id option_contract_id,c.strike,c.option_type,c.instrument_id,
                 i.provider_id,i.security_id,i.exchange_segment,i.instrument_type,i.symbol
          from public.underlying_assets u
          join public.instruments ui on ui.id=u.instrument_id
          join public.market_data_providers up on up.id=ui.provider_id and up.code='DHANHQ'
          join public.option_expiries e on e.underlying_id=u.id and e.expiry_date=:expiry and e.is_active
          join public.option_contracts c on c.underlying_id=u.id and c.option_expiry_id=e.id
            and c.is_active and not c.is_expired
          join public.instruments i on i.id=c.instrument_id and i.is_active
          join public.market_data_providers p on p.id=i.provider_id and p.code='DHANHQ'
          where upper(u.root_symbol)=:symbol and ui.is_active
            and ui.exchange_segment in ('IDX_I','NSE_EQ')
            and i.exchange_segment='NSE_FNO' and i.instrument_type in ('OPTIDX','OPTSTK')
          order by c.strike,c.option_type
        """), {"symbol": underlying.strip().upper(), "expiry": expiry})
        rows = [dict(row) for row in result.mappings()]
        return {"contracts": rows, "underlying": rows[0] if rows else None}

    async def underlying_momentum(self, instrument_id, *, max_age_seconds: int) -> dict | None:
        result = await self.session.execute(text("""
          select candle_timestamp,close_price from public.historical_candles
          where instrument_id=:instrument_id and timeframe='5m'
            and provider_id=(select id from public.market_data_providers where code='DHANHQ')
            and candle_timestamp >= now()-make_interval(secs => :age)
            and candle_timestamp <= now()
          order by candle_timestamp desc limit 2
        """), {"instrument_id": instrument_id, "age": max_age_seconds})
        rows = result.mappings().all()
        if len(rows) != 2:
            return None
        latest, previous = rows[0], rows[1]
        latest_close, previous_close = Decimal(latest["close_price"]), Decimal(previous["close_price"])
        if latest_close <= 0 or previous_close <= 0 or latest["candle_timestamp"] <= previous["candle_timestamp"]:
            return None
        pct = (latest_close - previous_close) / previous_close * Decimal(100)
        return {"change_pct": float(pct), "latest_timestamp": latest["candle_timestamp"],
                "previous_timestamp": previous["candle_timestamp"], "latest_close": latest_close,
                "previous_close": previous_close, "timeframe": "5m", "source": "DHANHQ historical candles"}

    async def historical_10x_many(self, *, underlying_id, contracts: list[tuple[Decimal, str]]) -> dict:
        if not contracts:
            return {}
        requests = json.dumps([{"strike": str(strike), "option_type": option_type.lower()}
                               for strike, option_type in contracts])
        result = await self.session.execute(text("""
          with requested as (
            select strike,option_type from jsonb_to_recordset(cast(:contracts as jsonb))
              as q(strike numeric,option_type text)
          ),
          valid as (
            select x.expiry_flag,x.expiry_code,x.strike_selection,x.provider_timestamp,
                   x.open_price,x.high_price,x.strike,x.option_type,x.interval,
                   (x.provider_timestamp at time zone 'UTC')::date source_day
            from public.expired_option_observations x
            join public.market_data_providers p on p.id=x.provider_id and p.code='DHANHQ'
            join requested q on q.strike=x.strike and q.option_type=x.option_type
            where x.underlying_id=:underlying_id and x.source='DHANHQ'
              and x.option_expiry_id is null and x.open_price>0
              and x.expiry_flag in ('WEEK','MONTH') and x.expiry_code in (0,1,2)
              and x.strike_selection is not null
              and x.provider_timestamp < now()
              and x.provider_timestamp >= now()-interval '5 years'
              and x.interval in ('1','5','15','25','60')
              and x.high_price is not null and x.high_price>=x.open_price
              and (x.low_price is null or (x.high_price>=x.low_price and x.low_price<=x.open_price))
          ), daily as (
            select expiry_flag,expiry_code,strike_selection,source_day,strike,option_type,
                   first_value(open_price) over(partition by expiry_flag,expiry_code,strike_selection,source_day,strike,option_type order by provider_timestamp) entry_price,
                   max(high_price) over(partition by expiry_flag,expiry_code,strike_selection,source_day,strike,option_type) max_price,
                   first_value(provider_timestamp) over(partition by expiry_flag,expiry_code,strike_selection,source_day,strike,option_type order by provider_timestamp) entry_timestamp,
                   first_value(provider_timestamp) over(partition by expiry_flag,expiry_code,strike_selection,source_day,strike,option_type order by high_price desc,provider_timestamp) peak_timestamp,
                   row_number() over(partition by expiry_flag,expiry_code,strike_selection,source_day,strike,option_type order by provider_timestamp) row_num
            from (
              select *,row_number() over(partition by expiry_flag,expiry_code,strike_selection,strike,option_type,provider_timestamp
                order by case interval when '1' then 1 when '5' then 2 when '15' then 3 when '25' then 4 else 5 end,
                         strike_selection nulls last) observation_rank
              from valid
            ) deduplicated where observation_rank=1
          )
          select strike,option_type,max(max_price/entry_price) maximum_multiple,
                 count(distinct source_day) source_days,
                 (array_agg(entry_timestamp order by max_price/entry_price desc))[1] baseline_timestamp,
                 (array_agg(peak_timestamp order by max_price/entry_price desc))[1] maximum_timestamp,
                 (array_agg(expiry_flag order by max_price/entry_price desc))[1] maximum_expiry_flag,
                 (array_agg(expiry_code order by max_price/entry_price desc))[1] maximum_expiry_code,
                 (array_agg(strike_selection order by max_price/entry_price desc))[1] maximum_strike_selection
          from daily where row_num=1 and entry_price>0 and max_price>0 group by strike,option_type
        """), {"underlying_id": underlying_id, "contracts": requests})
        return {(Decimal(row["strike"]), row["option_type"].upper()): {
            "available": True,
            "source": "DHANHQ expired-options rolling history",
            "maximum_multiple": Decimal(row["maximum_multiple"]), "source_days": row["source_days"],
            "baseline_timestamp": row["baseline_timestamp"],
            "maximum_timestamp": row["maximum_timestamp"],
            "maximum_expiry_flag": row["maximum_expiry_flag"],
            "maximum_expiry_code": row["maximum_expiry_code"],
            "maximum_strike_selection": row["maximum_strike_selection"],
            "method": "maximum same-source-day high divided by first stored positive open for the selected rolling expiry type/code and strike selection",
            "calendar_expiry_confirmed": False,
            "history_is_rolling_observation": True,
        } for row in result.mappings()}

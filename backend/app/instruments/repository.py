from datetime import date, datetime, timezone
from decimal import Decimal
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


class InstrumentRepository:
    """Persistence for shared provider mappings; user data is never queried here."""

    def __init__(self, session: AsyncSession):
        self.session = session

    async def ensure_provider(self) -> UUID:
        result = await self.session.execute(text("""
            insert into public.market_data_providers(code, display_name, provider_type, is_enabled)
            values ('DHANHQ', 'DhanHQ', 'market_data', true)
            on conflict (code) do update set is_enabled = true, updated_at = now()
            returning id
        """))
        return result.scalar_one()

    async def deactivate_provider_mappings(self, provider_id: UUID, now: datetime) -> None:
        await self.session.execute(text("""
            update public.instruments set is_active = false, inactive_at = :now
            where provider_id = :provider_id and is_active
        """), {"provider_id": provider_id, "now": now})
        await self.session.execute(text("""
            update public.option_contracts c set is_active = false, inactive_at = :now,
              is_expired = c.is_expired or e.expiry_date < (:now at time zone 'utc')::date
            from public.instruments i, public.option_expiries e
            where c.instrument_id = i.id and c.option_expiry_id = e.id
              and i.provider_id = :provider_id and c.is_active
        """), {"provider_id": provider_id, "now": now})
        await self.session.execute(text("""
            update public.option_expiries e set is_active = false
            from public.underlying_assets u join public.instruments i on i.id = u.instrument_id
            where e.underlying_id = u.id and i.provider_id = :provider_id and i.is_active = false
        """), {"provider_id": provider_id})

    async def upsert_underlying(self, provider_id: UUID, item: dict, synchronized_at: datetime) -> UUID:
        result = await self.session.execute(text("""
            insert into public.instruments
              (provider_id, exchange_segment, security_id, symbol, display_name, asset_type,
               instrument_type, underlying_symbol, tick_size, lot_size, source, synchronized_at,
               is_active, inactive_at, metadata)
            values (:provider_id, :exchange_segment, :security_id, :symbol, :display_name, :asset_type,
                    :instrument_type, :underlying_symbol, :tick_size, :lot_size, 'DHANHQ', :synchronized_at,
                    true, null, cast(:metadata as jsonb))
            on conflict (provider_id, exchange_segment, security_id) do update set
              symbol = excluded.symbol, display_name = excluded.display_name, asset_type = excluded.asset_type,
              instrument_type = excluded.instrument_type, underlying_symbol = excluded.underlying_symbol,
              tick_size = excluded.tick_size, lot_size = excluded.lot_size, source = 'DHANHQ',
              synchronized_at = excluded.synchronized_at, is_active = true, inactive_at = null,
              metadata = excluded.metadata, updated_at = now()
            returning id
        """), {**item, "provider_id": provider_id, "synchronized_at": synchronized_at})
        instrument_id = result.scalar_one()
        root_type = "index" if item["instrument_type"] == "INDEX" else "equity"
        result = await self.session.execute(text("""
            insert into public.underlying_assets(instrument_id, underlying_type, root_symbol, metadata)
            values (:instrument_id, :underlying_type, :root_symbol, cast(:metadata as jsonb))
            on conflict (instrument_id) do update set underlying_type = excluded.underlying_type,
              root_symbol = excluded.root_symbol, metadata = excluded.metadata, updated_at = now()
            returning id
        """), {"instrument_id": instrument_id, "underlying_type": root_type,
               "root_symbol": item["root_symbol"], "metadata": item["metadata"]})
        return result.scalar_one()

    async def underlying_by_security(self, provider_id: UUID, segment: str, security_id: str):
        result = await self.session.execute(text("""
            select u.id as underlying_id, i.id as instrument_id, i.security_id, i.exchange_segment,
                   i.symbol, i.display_name, i.instrument_type, u.root_symbol
            from public.instruments i join public.underlying_assets u on u.instrument_id = i.id
            where i.provider_id = :provider_id and i.exchange_segment = :segment
              and i.security_id = :security_id and i.is_active
        """), {"provider_id": provider_id, "segment": segment, "security_id": security_id})
        return result.mappings().one_or_none()

    async def upsert_option(self, provider_id: UUID, underlying_id: UUID, underlying_symbol: str,
                            expiry: date, item: dict, synchronized_at: datetime) -> UUID:
        result = await self.session.execute(text("""
            insert into public.instruments
              (provider_id, exchange_segment, security_id, symbol, display_name, asset_type,
               instrument_type, underlying_symbol, expiry_date, strike_price, option_type,
               tick_size, lot_size, source, synchronized_at, is_active, inactive_at, metadata)
            values (:provider_id, :exchange_segment, :security_id, :symbol, :display_name, 'option',
                    :instrument_type, :underlying_symbol, :expiry_date, :strike_price, :option_type,
                    :tick_size, :lot_size, 'DHANHQ', :synchronized_at, :is_active, :inactive_at, cast(:metadata as jsonb))
            on conflict (provider_id, exchange_segment, security_id) do update set
              symbol = excluded.symbol, display_name = excluded.display_name, asset_type = 'option',
              instrument_type = excluded.instrument_type, underlying_symbol = excluded.underlying_symbol,
              expiry_date = excluded.expiry_date, strike_price = excluded.strike_price, option_type = excluded.option_type,
              tick_size = excluded.tick_size, lot_size = excluded.lot_size, source = 'DHANHQ',
              synchronized_at = excluded.synchronized_at, is_active = excluded.is_active,
              inactive_at = excluded.inactive_at, metadata = excluded.metadata, updated_at = now()
            returning id
        """), {**item, "provider_id": provider_id, "underlying_symbol": underlying_symbol,
               "synchronized_at": synchronized_at})
        instrument_id = result.scalar_one()
        expiry_result = await self.session.execute(text("""
            insert into public.option_expiries(underlying_id, expiry_date, expiry_kind, is_active, source, synchronized_at)
            values (:underlying_id, :expiry_date, :expiry_kind, :is_active, 'DHANHQ', :synchronized_at)
            on conflict (underlying_id, expiry_date, expiry_kind) do update set
              is_active = excluded.is_active, source = 'DHANHQ', synchronized_at = excluded.synchronized_at, updated_at = now()
            returning id
        """), {"underlying_id": underlying_id, "expiry_date": item["expiry_date"],
               "expiry_kind": item["expiry_kind"], "is_active": item["is_active"], "synchronized_at": synchronized_at})
        expiry_id = expiry_result.scalar_one()
        await self.session.execute(text("""
            insert into public.option_contracts
              (instrument_id, underlying_id, option_expiry_id, strike, option_type, contract_symbol,
               lot_size, is_active, is_expired, source, synchronized_at, inactive_at)
            values (:instrument_id, :underlying_id, :expiry_id, :strike, :option_type, :contract_symbol,
                    :lot_size, :is_active, :is_expired, 'DHANHQ', :synchronized_at, :inactive_at)
            on conflict (underlying_id, option_expiry_id, strike, option_type) do update set
              instrument_id = excluded.instrument_id, contract_symbol = excluded.contract_symbol,
              lot_size = excluded.lot_size, is_active = excluded.is_active, is_expired = excluded.is_expired,
              source = 'DHANHQ', synchronized_at = excluded.synchronized_at,
              inactive_at = excluded.inactive_at, updated_at = now()
        """), {"instrument_id": instrument_id, "underlying_id": underlying_id,
               "expiry_id": expiry_id, "strike": item["strike_price"], "option_type": item["option_type"].lower(),
               "contract_symbol": item["display_name"], "lot_size": item["lot_size"],
               "is_active": item["is_active"], "is_expired": item["is_expired"],
               "synchronized_at": synchronized_at, "inactive_at": item["inactive_at"]})
        return instrument_id

    async def list_instruments(self, *, symbol: str | None, search: str | None, include_inactive: bool, limit: int, offset: int):
        result = await self.session.execute(text("""
            select i.id, i.exchange_segment, i.security_id, i.symbol, i.display_name, i.asset_type,
                   i.instrument_type, i.underlying_symbol, i.expiry_date, i.strike_price, i.option_type,
                   i.lot_size, i.tick_size, i.source, i.synchronized_at, i.is_active, i.inactive_at,
                   (i.expiry_date is not null and i.expiry_date < (now() at time zone 'utc')::date) as is_expired
            from public.instruments i join public.market_data_providers p on p.id = i.provider_id
            where p.code = 'DHANHQ' and (:include_inactive or i.is_active)
              and (:symbol is null or upper(coalesce(i.underlying_symbol, i.symbol)) = :symbol
                   or upper(i.symbol) = :symbol)
              and (:search is null or i.symbol ilike :pattern or i.display_name ilike :pattern
                   or i.underlying_symbol ilike :pattern)
            order by i.underlying_symbol nulls first, i.expiry_date nulls first, i.strike_price nulls first, i.option_type nulls first
            limit :limit offset :offset
        """), {"symbol": symbol, "search": search, "pattern": f"%{search}%" if search else None,
               "include_inactive": include_inactive, "limit": limit, "offset": offset})
        return [dict(row) for row in result.mappings()]

    async def resolve_underlying(self, symbol: str):
        result = await self.session.execute(text("""
            select u.id as underlying_id, u.root_symbol, i.id as instrument_id,
                   i.security_id, i.exchange_segment, i.symbol, i.instrument_type, i.is_active
            from public.underlying_assets u join public.instruments i on i.id = u.instrument_id
            join public.market_data_providers p on p.id = i.provider_id
            where p.code = 'DHANHQ' and upper(u.root_symbol) = :symbol
              and i.is_active and i.exchange_segment in ('IDX_I','NSE_EQ')
            order by case when i.exchange_segment = 'IDX_I' then 0 else 1 end limit 1
        """), {"symbol": symbol})
        return result.mappings().one_or_none()

    async def has_option_expiry(self, underlying_id: UUID, expiry: date) -> bool:
        result = await self.session.execute(text("""
            select exists (
              select 1 from public.option_expiries e
              join public.option_contracts c on c.option_expiry_id = e.id
              where e.underlying_id = :underlying_id and e.expiry_date = :expiry and e.is_active
                and c.is_active and not c.is_expired
            )
        """), {"underlying_id": underlying_id, "expiry": expiry})
        return bool(result.scalar_one())

    async def list_option_underlyings(self):
        result = await self.session.execute(text("""
            select distinct u.root_symbol as symbol, i.exchange_segment, i.instrument_type
            from public.underlying_assets u
            join public.instruments i on i.id = u.instrument_id
            join public.market_data_providers p on p.id = i.provider_id
            join public.option_contracts c on c.underlying_id = u.id
            where p.code = 'DHANHQ' and i.is_active and c.is_active and not c.is_expired
              and i.exchange_segment in ('IDX_I','NSE_EQ')
            order by u.root_symbol
        """))
        return [dict(row) for row in result.mappings()]

    async def resolve_option(self, underlying: str, expiry: date, strike: Decimal, option_type: str):
        result = await self.session.execute(text("""
            select i.id, i.provider_id, i.exchange_segment, i.security_id, i.instrument_type,
                   i.underlying_symbol, i.expiry_date, i.strike_price, i.option_type,
                   i.lot_size, i.tick_size, i.source, i.synchronized_at
            from public.instruments i join public.market_data_providers p on p.id = i.provider_id
            where p.code = 'DHANHQ' and i.is_active and i.exchange_segment = 'NSE_FNO'
              and i.underlying_symbol = :underlying and i.expiry_date = :expiry
              and i.strike_price = :strike and i.option_type = :option_type
              and i.instrument_type in ('OPTIDX','OPTSTK')
            limit 1
        """), {"underlying": underlying, "expiry": expiry, "strike": strike, "option_type": option_type})
        return result.mappings().one_or_none()

    async def record_expiries(self, underlying_id: UUID, expiry_dates: list[date]) -> None:
        now = datetime.now(timezone.utc)
        await self.session.execute(text("""
            update public.option_expiries set is_active = false, updated_at = now()
            where underlying_id = :underlying_id
        """), {"underlying_id": underlying_id})
        for expiry in expiry_dates:
            updated = await self.session.execute(text("""
                update public.option_expiries set is_active = :is_active, source = 'DHANHQ', synchronized_at = :synchronized_at, updated_at = now()
                where underlying_id = :underlying_id and expiry_date = :expiry_date
            """), {"underlying_id": underlying_id, "expiry_date": expiry, "is_active": expiry >= now.date(), "synchronized_at": now})
            if updated.rowcount == 0:
                await self.session.execute(text("""
                    insert into public.option_expiries(underlying_id, expiry_date, expiry_kind, is_active, source, synchronized_at)
                    values (:underlying_id, :expiry_date, 'regular', :is_active, 'DHANHQ', :synchronized_at)
                    on conflict (underlying_id, expiry_date, expiry_kind) do update set is_active = excluded.is_active, source = 'DHANHQ', synchronized_at = excluded.synchronized_at, updated_at = now()
                """), {"underlying_id": underlying_id, "expiry_date": expiry, "is_active": expiry >= now.date(), "synchronized_at": now})

    async def commit(self):
        await self.session.commit()

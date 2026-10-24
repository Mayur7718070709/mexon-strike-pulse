import json
import math
from datetime import datetime, timezone
from decimal import Decimal
from uuid import UUID

from sqlalchemy import text


class WatchlistService:
    """Owner-scoped watchlists. Shared prices are projected only from Dhan Redis observations."""

    def __init__(self, session, redis, *, stale_after_seconds: int = 15):
        self.session = session
        self.redis = redis
        self.stale_after_seconds = stale_after_seconds

    async def list_for_user(self, user_id: UUID) -> list[dict]:
        lists = await self.session.execute(text("""
          select id,name,description,is_default,created_at,updated_at
          from public.watchlists where user_id=:user_id
          order by is_default desc,created_at,id
        """), {"user_id": user_id})
        result = []
        for watchlist in lists.mappings():
            items = await self.session.execute(text("""
              select wi.id item_id,wi.display_order,wi.note,wi.created_at item_created_at,
                     wi.instrument_id,wi.option_contract_id,
                     i.exchange_segment,i.security_id,i.symbol,i.display_name,i.underlying_symbol,
                     i.expiry_date,i.strike_price,i.option_type,
                     oi.exchange_segment oi_exchange_segment,oi.security_id oi_security_id,
                     oi.display_name oi_display_name,
                     u.root_symbol,e.expiry_date contract_expiry,c.strike contract_strike,
                     c.option_type contract_option_type
              from public.watchlist_items wi
              left join public.instruments i on i.id=wi.instrument_id
              left join public.option_contracts c on c.id=wi.option_contract_id
              left join public.instruments oi on oi.id=c.instrument_id
              left join public.underlying_assets u on u.id=c.underlying_id
              left join public.option_expiries e on e.id=c.option_expiry_id
              where wi.watchlist_id=:watchlist_id and wi.user_id=:user_id
              order by wi.display_order,wi.created_at
            """), {"watchlist_id": watchlist["id"], "user_id": user_id})
            output_items = []
            for row in items.mappings():
                instrument = row["instrument_id"] is not None
                segment = row["exchange_segment"] if instrument else self._row_value(row, "oi_exchange_segment")
                security_id = row["security_id"] if instrument else self._row_value(row, "oi_security_id")
                quote = await self._read_quote(segment, security_id) if segment and security_id else None
                if instrument:
                    underlying = row["underlying_symbol"] or row["symbol"]
                    expiry, strike, option_type = row["expiry_date"], row["strike_price"], row["option_type"]
                    display_name = row["display_name"]
                else:
                    underlying = row["root_symbol"]
                    expiry, strike, option_type = row["contract_expiry"], row["contract_strike"], row["contract_option_type"]
                    display_name = self._row_value(row, "oi_display_name")
                output_items.append({
                    "id": str(row["item_id"]), "instrument_id": str(row["instrument_id"]) if row["instrument_id"] else None,
                    "option_contract_id": str(row["option_contract_id"]) if row["option_contract_id"] else None,
                    "underlying": underlying, "expiry": expiry, "strike": strike,
                    "option_type": str(option_type).upper() if option_type else None,
                    "display_name": display_name, "exchange_segment": segment, "security_id": security_id,
                    "note": row["note"], "display_order": row["display_order"],
                    "quote": self._quote_projection(quote), "created_at": row["item_created_at"],
                })
            result.append({**dict(watchlist), "id": str(watchlist["id"]), "items": output_items})
        return result

    async def create(self, *, user_id: UUID, name: str, description: str | None, is_default: bool) -> dict:
        try:
            result = await self.session.execute(text("""
              insert into public.watchlists(user_id,name,description,is_default)
              values (:user_id,:name,:description,:is_default)
              returning id,name,description,is_default,created_at,updated_at
            """), {"user_id": user_id, "name": name, "description": description, "is_default": is_default})
            row = result.mappings().one()
            await self.session.commit()
            return {**dict(row), "id": str(row["id"]), "items": []}
        except Exception:
            await self.session.rollback()
            raise

    async def add_item(self, *, user_id: UUID, watchlist_id: UUID, instrument_id: UUID | None,
                       option_contract_id: UUID | None, underlying: str | None = None,
                       expiry=None, strike: Decimal | None = None, option_type: str | None = None,
                       note: str | None = None) -> dict:
        owner = await self.session.execute(text("""
          select id from public.watchlists where id=:id and user_id=:user_id
        """), {"id": watchlist_id, "user_id": user_id})
        if owner.scalar_one_or_none() is None:
            raise LookupError("Watchlist not found")

        if underlying is not None:
            contract = await self.session.execute(text("""
              select c.id option_contract_id,i.id instrument_id,i.exchange_segment,i.security_id
              from public.option_contracts c
              join public.instruments i on i.id=c.instrument_id
              join public.underlying_assets u on u.id=c.underlying_id
              join public.option_expiries e on e.id=c.option_expiry_id and e.underlying_id=c.underlying_id
              join public.market_data_providers p on p.id=i.provider_id
              where p.code='DHANHQ' and i.exchange_segment='NSE_FNO'
                and i.instrument_type in ('OPTIDX','OPTSTK') and i.is_active
                and c.is_active and not c.is_expired and e.is_active
                and (e.expiry_date > (now() at time zone 'Asia/Kolkata')::date
                  or (e.expiry_date = (now() at time zone 'Asia/Kolkata')::date
                    and (now() at time zone 'Asia/Kolkata')::time < time '15:30'))
                and upper(u.root_symbol)=:underlying and e.expiry_date=:expiry
                and c.strike=:strike and upper(c.option_type)=:option_type
              limit 1
            """), {"underlying": underlying.upper(), "expiry": expiry, "strike": strike,
                   "option_type": option_type.upper()})
            mapped = contract.mappings().one_or_none()
            if mapped is None:
                raise ValueError("No active Dhan-confirmed contract matches the requested option identity")
            option_contract_id = mapped["option_contract_id"]
            instrument_id = None
            exchange_segment, security_id = mapped["exchange_segment"], str(mapped["security_id"])
        else:
            mapped = await self._resolve_subject(instrument_id, option_contract_id)
            exchange_segment, security_id = mapped["exchange_segment"], str(mapped["security_id"])

        try:
            result = await self.session.execute(text("""
              insert into public.watchlist_items(user_id,watchlist_id,instrument_id,option_contract_id,note)
              values (:user_id,:watchlist_id,:instrument_id,:option_contract_id,:note)
              on conflict do nothing returning id
            """), {"user_id": user_id, "watchlist_id": watchlist_id,
                   "instrument_id": instrument_id, "option_contract_id": option_contract_id, "note": note})
            item_id = result.scalar_one_or_none()
            if item_id is None:
                existing = await self.session.execute(text("""
                  select id from public.watchlist_items where user_id=:user_id and watchlist_id=:watchlist_id
                    and ((:instrument_id is not null and instrument_id=:instrument_id)
                      or (:option_contract_id is not null and option_contract_id=:option_contract_id))
                """), {"user_id": user_id, "watchlist_id": watchlist_id,
                       "instrument_id": instrument_id, "option_contract_id": option_contract_id})
                item_id = existing.scalar_one()
            await self.session.commit()
        except Exception:
            await self.session.rollback()
            raise

        quote = await self._read_quote(exchange_segment, security_id)
        if self._quote_projection(quote)["status"] != "LIVE":
            try:
                await self.redis.sadd("market:subscriptions:validated", f"{exchange_segment}:{security_id}")
                await self.redis.expire("market:subscriptions:validated", 15)
                await self.redis.publish("market:subscriptions:add", json.dumps({
                    "exchange_segment": exchange_segment, "security_id": security_id, "source": "DHANHQ"}))
            except Exception:
                # The item is valid and persisted, but quote state stays unavailable.
                pass
        return {"id": str(item_id), "exchange_segment": exchange_segment, "security_id": security_id,
                "quote": self._quote_projection(quote)}

    async def remove_item(self, *, user_id: UUID, watchlist_id: UUID, item_id: UUID) -> bool:
        result = await self.session.execute(text("""
          delete from public.watchlist_items where id=:item_id and watchlist_id=:watchlist_id and user_id=:user_id
          returning id
        """), {"item_id": item_id, "watchlist_id": watchlist_id, "user_id": user_id})
        await self.session.commit()
        return result.scalar_one_or_none() is not None

    async def _resolve_subject(self, instrument_id: UUID | None, option_contract_id: UUID | None):
        if instrument_id is not None:
            result = await self.session.execute(text("""
              select i.exchange_segment,i.security_id from public.instruments i
              join public.market_data_providers p on p.id=i.provider_id
              where i.id=:id and p.code='DHANHQ' and i.is_active
                and i.exchange_segment in ('IDX_I','NSE_EQ')
            """), {"id": instrument_id})
        else:
            result = await self.session.execute(text("""
              select i.exchange_segment,i.security_id from public.option_contracts c
              join public.instruments i on i.id=c.instrument_id
              join public.option_expiries e on e.id=c.option_expiry_id
              join public.market_data_providers p on p.id=i.provider_id
              where c.id=:id and p.code='DHANHQ' and i.is_active and c.is_active and not c.is_expired and e.is_active
                and (e.expiry_date > (now() at time zone 'Asia/Kolkata')::date
                  or (e.expiry_date = (now() at time zone 'Asia/Kolkata')::date
                    and (now() at time zone 'Asia/Kolkata')::time < time '15:30'))
            """), {"id": option_contract_id})
        row = result.mappings().one_or_none()
        if row is None:
            raise ValueError("Watchlist items must reference an active Dhan-confirmed instrument or option contract")
        return row

    async def _read_quote(self, segment: str, security_id: str) -> dict | None:
        try:
            raw = await self.redis.get(f"market:quote:{segment}:{security_id}")
            quote = json.loads(raw) if raw else None
            if not isinstance(quote, dict) or quote.get("source") != "DHANHQ":
                return None
            if quote.get("exchange_segment") != segment or str(quote.get("security_id")) != str(security_id):
                return None
            return quote
        except Exception:
            return None

    def _quote_projection(self, quote: dict | None) -> dict:
        if quote is None:
            return {"status": "DATA UNAVAILABLE", "source": "DHANHQ", "last_price": None,
                    "provider_timestamp": None, "ingested_at": None}
        provider = self._parse_time(quote.get("exchange_timestamp"))
        ingested = self._parse_time(quote.get("received_at") or quote.get("ingested_at"))
        price = quote.get("last_price")
        valid_price = isinstance(price, (int, float)) and math.isfinite(price) and price > 0
        age = max((datetime.now(timezone.utc) - provider).total_seconds(),
                  (datetime.now(timezone.utc) - ingested).total_seconds()) if provider and ingested else None
        fresh = valid_price and age is not None and -2 <= age <= self.stale_after_seconds
        return {"status": "LIVE" if fresh else "DATA STALE" if valid_price else "DATA UNAVAILABLE", "source": "DHANHQ",
                "last_price": price if valid_price else None,
                "provider_timestamp": quote.get("exchange_timestamp"),
                "ingested_at": quote.get("received_at") or quote.get("ingested_at"), "age_seconds": age}

    @staticmethod
    def _parse_time(value):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return parsed.astimezone(timezone.utc) if parsed.tzinfo else None
        except (AttributeError, TypeError, ValueError):
            return None

    @staticmethod
    def _row_value(row, key):
        try:
            return row[key]
        except (KeyError, TypeError):
            return None

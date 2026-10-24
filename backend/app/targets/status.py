import json
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from uuid import UUID

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.targets.calculation import TargetCalculationService

log = structlog.get_logger(__name__)


class TargetDataUnavailable(RuntimeError):
    pass


class TargetStatusService:
    """Persist target levels and evaluate them only against fresh Dhan observations."""

    def __init__(self, session: AsyncSession, redis, *, stale_after_seconds: int = 15):
        self.session = session
        self.redis = redis
        self.stale_after_seconds = stale_after_seconds

    async def start_session(self, *, user_id: UUID, request_id: UUID, underlying: str,
                            expiry, strike: Decimal, option_type: str,
                            confirmed_reference_premium: Decimal | None = None,
                            confirmed_reference_timestamp: datetime | None = None) -> dict:
        existing = await self.session.execute(text("""
          select id from public.target_tracking_sessions where user_id=:user_id and start_request_key=:request_id
        """), {"user_id": user_id, "request_id": request_id})
        existing_id = existing.scalar_one_or_none()
        if existing_id is not None:
            return await self.get_session(session_id=existing_id, user_id=user_id)

        contract_result = await self.session.execute(text("""
          select c.id
          from public.option_contracts c
          join public.instruments i on i.id=c.instrument_id
          join public.option_expiries e on e.id=c.option_expiry_id and e.underlying_id=c.underlying_id
          join public.underlying_assets u on u.id=c.underlying_id
          join public.market_data_providers p on p.id=i.provider_id
          where p.code='DHANHQ' and i.exchange_segment='NSE_FNO'
            and i.instrument_type in ('OPTIDX','OPTSTK') and i.is_active
            and c.is_active and not c.is_expired and e.is_active
            and upper(u.root_symbol)=:underlying and e.expiry_date=:expiry
            and c.strike=:strike and upper(c.option_type)=:option_type
            and (e.expiry_date > (now() at time zone 'Asia/Kolkata')::date
              or (e.expiry_date = (now() at time zone 'Asia/Kolkata')::date
                and (now() at time zone 'Asia/Kolkata')::time < time '15:30'))
          limit 1
        """), {"underlying": underlying.strip().upper(), "expiry": expiry,
               "strike": strike, "option_type": option_type.upper()})
        contract_id = contract_result.scalar_one_or_none()
        if contract_id is None:
            raise ValueError("No active, unexpired Dhan-confirmed option contract matches this identity")
        return await self.create_session(
            user_id=user_id, option_contract_id=contract_id,
            confirmed_reference_premium=confirmed_reference_premium,
            confirmed_reference_timestamp=confirmed_reference_timestamp,
            require_confirmation=True, start_request_key=request_id,
        )

    async def create_session(self, *, user_id: UUID, option_contract_id: UUID,
                             confirmed_reference_premium: Decimal | None = None,
                             confirmed_reference_timestamp: datetime | None = None,
                             require_confirmation: bool = False,
                             start_request_key: UUID | None = None) -> dict:
        contract_result = await self.session.execute(text("""
          select c.id option_contract_id,c.underlying_id,c.option_expiry_id,c.strike,c.option_type,
                 i.id instrument_id,i.provider_id,i.exchange_segment,i.security_id,i.symbol,
                 i.display_name,e.expiry_date,u.root_symbol
          from public.option_contracts c
          join public.instruments i on i.id=c.instrument_id
          join public.option_expiries e on e.id=c.option_expiry_id and e.underlying_id=c.underlying_id
          join public.underlying_assets u on u.id=c.underlying_id
          join public.market_data_providers p on p.id=i.provider_id
          where c.id=:contract_id and c.is_active and not c.is_expired and i.is_active and e.is_active
            and i.exchange_segment='NSE_FNO' and i.instrument_type in ('OPTIDX','OPTSTK')
            and p.code='DHANHQ'
        """), {"contract_id": option_contract_id})
        contract = contract_result.mappings().one_or_none()
        if contract is None:
            raise ValueError("No active Dhan-confirmed NSE option contract matches this contract ID")
        option_type = str(contract["option_type"]).upper()
        if option_type not in {"CALL", "PUT"}:
            raise ValueError("The Dhan instrument mapping has an unsupported option type")

        segment, security_id = contract["exchange_segment"], str(contract["security_id"])
        quote = await self._read_quote(segment, security_id)
        if (quote is None or quote.get("exchange_segment") != segment
                or str(quote.get("security_id")) != security_id or self._freshness(quote) != "FRESH"):
            # The ID is source-confirmed by the synced instrument master. Ask the existing
            # Dhan feed to subscribe, but do not create a session without a live reference.
            try:
                await self.redis.sadd("market:subscriptions:validated", f"{segment}:{security_id}")
                await self.redis.expire("market:subscriptions:validated", 15)
                await self.redis.publish("market:subscriptions:add", json.dumps({
                    "exchange_segment": segment, "security_id": security_id, "source": "DHANHQ"}))
            except Exception as exc:
                raise TargetDataUnavailable("Live Dhan data is unavailable; no target session was created") from exc
            raise TargetDataUnavailable("Waiting for a fresh Dhan option quote; no target session was created")

        try:
            reference = TargetCalculationService.validate_reference_premium(quote["last_price"])
            levels = TargetCalculationService.calculate(reference, option_type)
            provider_timestamp = self._timestamp(quote.get("exchange_timestamp"))
            ingested_at = self._timestamp(quote.get("received_at") or quote.get("ingested_at"))
        except (ValueError, TypeError) as exc:
            raise TargetDataUnavailable("Dhan returned an invalid live reference observation") from exc
        source_observation = {"provider": "DHANHQ", "source": "live_market_feed",
                              "security_id": security_id, "exchange_segment": segment,
                              "provider_timestamp": provider_timestamp.isoformat(),
                              "received_at": ingested_at.isoformat(),
                              "last_price": str(reference)}
        preview = {"status": "AWAITING_REFERENCE_CONFIRMATION", "confirmation_required": True,
                   "request_id": str(start_request_key) if start_request_key else None,
                   "underlying": contract["root_symbol"], "expiry": contract["expiry_date"],
                   "strike": contract["strike"], "option_type": option_type,
                   "reference_premium": reference, "reference_timestamp": provider_timestamp,
                   "ingested_at": ingested_at, "source_observation": source_observation,
                   "targets": [level.model_dump() for level in TargetCalculationService.calculate(reference, option_type)],
                   "message": "Confirm this current Dhan observation to create an immutable tracking baseline."}
        if require_confirmation and (confirmed_reference_premium is None or confirmed_reference_timestamp is None):
            return preview
        if require_confirmation:
            try:
                confirmed_premium = TargetCalculationService.validate_reference_premium(confirmed_reference_premium)
            except (ValueError, TypeError) as exc:
                raise ValueError("Confirmed premium is invalid") from exc
            confirmed_timestamp = self._timestamp(confirmed_reference_timestamp.isoformat())
            if confirmed_premium != reference or confirmed_timestamp != provider_timestamp:
                return {**preview, "message": "The Dhan observation changed after it was shown. Confirm the newly displayed premium and timestamp."}
        result = await self.session.execute(text("""
          insert into public.target_tracking_sessions
            (user_id,instrument_id,option_contract_id,direction,target_price,status,starts_at,
             reference_premium,reference_timestamp,underlying_id,option_expiry_id,strike,
             option_type,source_observation,verification_status,latest_observed_premium,
             latest_observation_timestamp,latest_observation_ingested_at,current_multiple,start_request_key,metadata)
          values (:user_id,:instrument_id,:contract_id,'above',:legacy_target,'active',:provider_timestamp,
             :reference,:provider_timestamp,:underlying_id,:expiry_id,:strike,:option_type,
             cast(:source_observation as jsonb),'verified',:reference,:provider_timestamp,:ingested_at,1,
             :start_request_key,cast(:metadata as jsonb))
          on conflict (user_id,start_request_key) where start_request_key is not null do nothing
          returning id,created_at
        """), {"user_id": user_id, "instrument_id": contract["instrument_id"],
               "contract_id": contract["option_contract_id"],
               "legacy_target": levels[0].target_value, "provider_timestamp": provider_timestamp,
               "reference": reference, "underlying_id": contract["underlying_id"],
               "expiry_id": contract["option_expiry_id"], "strike": contract["strike"],
               "option_type": option_type.lower(), "source_observation": json.dumps(source_observation),
               "ingested_at": ingested_at, "start_request_key": start_request_key,
               "metadata": json.dumps({"source": "DHANHQ", "target_kind": "premium_multiple",
                                       "target_semantics": "tracking level, not a prediction or guarantee"})})
        session_row = result.mappings().one_or_none()
        if session_row is None and start_request_key is not None:
            await self.session.rollback()
            prior = await self.session.execute(text("""
              select id from public.target_tracking_sessions where user_id=:user_id and start_request_key=:request_key
            """), {"user_id": user_id, "request_key": start_request_key})
            prior_id = prior.scalar_one_or_none()
            if prior_id is not None:
                return await self.get_session(session_id=prior_id, user_id=user_id)
            raise TargetDataUnavailable("The tracking request could not be committed; please retry with the same request ID")
        await self.session.execute(text("""
          insert into public.target_tracking_levels
            (session_id,user_id,multiplier,target_value,status,last_observed_at,last_observation)
          values (:session_id,:user_id,:multiplier,:target_value,'NOT_REACHED',:provider_timestamp,
                  cast(:observation as jsonb))
        """), [{"session_id": session_row["id"], "user_id": user_id,
                "multiplier": level.multiplier, "target_value": level.target_value,
                "provider_timestamp": provider_timestamp,
                "observation": json.dumps(source_observation)} for level in levels])
        await self.session.commit()
        # The worker reconciles this set from active DB sessions as well, but
        # publish immediately so this contract can enter the Dhan feed now.
        pair = f"{segment}:{security_id}"
        try:
            await self.redis.sadd("market:target:subscriptions", pair)
            await self.redis.sadd("market:subscriptions:validated", pair)
            await self.redis.expire("market:target:subscriptions", 15)
            await self.redis.expire("market:subscriptions:validated", 15)
            await self.redis.publish("market:subscriptions:add", json.dumps({
                "exchange_segment": segment, "security_id": security_id, "source": "DHANHQ"}))
        except Exception as exc:
            # The session is already committed; the monitor's DB reconciliation
            # will restore the subscription once Redis recovers.
            log.error("target_monitor_subscription_publish_failed", error_type=type(exc).__name__)
        return {"session_id": str(session_row["id"]), "status": "active", "confirmation_required": False,
                "instrument": contract["display_name"], "underlying": contract["root_symbol"],
                "expiry": contract["expiry_date"], "strike": contract["strike"],
                "option_type": option_type, "reference_premium": reference,
                "reference_timestamp": provider_timestamp, "latest_observed_premium": reference,
                "latest_observation_timestamp": provider_timestamp, "latest_observation_ingested_at": ingested_at,
                "current_multiple": Decimal("1"), "data_quality": "LIVE", "source_observation": source_observation,
                "targets": [level.model_dump() for level in levels],
                "message": "Targets are observation levels for tracking, not predictions or guarantees."}

    async def process_live_observation(self, observation: dict) -> int:
        if observation.get("source") != "DHANHQ":
            return 0
        segment = observation.get("exchange_segment")
        security_id = str(observation.get("security_id", ""))
        if segment != "NSE_FNO" or not security_id.isdigit():
            return 0
        fresh = self._freshness(observation) == "FRESH"
        provider_time = self._timestamp(observation.get("exchange_timestamp"))
        received_time = self._timestamp(observation.get("received_at") or observation.get("ingested_at"))
        try:
            price = TargetCalculationService.validate_reference_premium(observation.get("last_price"))
        except (ValueError, TypeError):
            fresh = False
            price = None
        source_observation = {"provider": "DHANHQ", "source": "live_market_feed",
                              "security_id": security_id, "exchange_segment": segment,
                              "provider_timestamp": provider_time.isoformat(),
                              "received_at": received_time.isoformat(),
                              "last_price": str(price) if price is not None else None}
        result = await self.session.execute(text("""
          select t.id session_id,t.user_id,l.id level_id,l.multiplier,l.target_value,l.first_reached_at,
                 t.reference_premium,t.latest_observation_timestamp,t.latest_observation_ingested_at
          from public.target_tracking_sessions t
          join public.option_contracts c on c.id=t.option_contract_id
          join public.instruments i on i.id=c.instrument_id
          join public.option_expiries e on e.id=c.option_expiry_id
          join public.target_tracking_levels l on l.session_id=t.id and l.user_id=t.user_id
          where t.status='active' and i.provider_id=(select id from public.market_data_providers where code='DHANHQ')
            and i.exchange_segment=:segment and i.security_id=:security_id
            and (e.expiry_date > (now() at time zone 'Asia/Kolkata')::date
              or (e.expiry_date = (now() at time zone 'Asia/Kolkata')::date
                and (now() at time zone 'Asia/Kolkata')::time < time '15:30'))
        """), {"segment": segment, "security_id": security_id})
        rows = result.mappings().all()
        session_ids = {row["session_id"] for row in rows}
        for session_id in session_ids:
            await self.session.execute(text("""
              update public.target_tracking_sessions set verification_status=:verification,
                latest_observed_premium=case when :price is not null and :verification='verified' and
                  (latest_observation_timestamp is null or latest_observation_timestamp <= :provider_timestamp)
                  then :price else latest_observed_premium end,
                latest_observation_timestamp=case when :price is not null and :verification='verified' and
                  (latest_observation_timestamp is null or latest_observation_timestamp <= :provider_timestamp)
                  then :provider_timestamp else latest_observation_timestamp end,
                latest_observation_ingested_at=case when :price is not null and :verification='verified' and
                  (latest_observation_timestamp is null or latest_observation_timestamp <= :provider_timestamp)
                  then :received_timestamp else latest_observation_ingested_at end,
                current_multiple=case when :price is not null and :verification='verified' and reference_premium > 0 and
                  (latest_observation_timestamp is null or latest_observation_timestamp <= :provider_timestamp)
                  then :price/reference_premium else current_multiple end,
                updated_at=now()
              where id=:session_id and status='active'
                and (latest_observation_timestamp is null or latest_observation_timestamp < :provider_timestamp
                  or (latest_observation_timestamp=:provider_timestamp and
                    (latest_observation_ingested_at is null or latest_observation_ingested_at < :received_timestamp))
                  or :verification='unverified')
            """), {"verification": "verified" if fresh else "unverified", "session_id": session_id,
                   "price": price, "provider_timestamp": provider_time,
                   "received_timestamp": received_time})
        for row in rows:
            if row["first_reached_at"] is not None:
                continue  # A verified first hit is permanent; later stale data cannot clear it.
            # The live socket observation must be strictly newer than the last
            # source observation. Replayed/stale stream messages cannot create hits.
            latest_provider_time = (self._timestamp(row["latest_observation_timestamp"])
                                    if row["latest_observation_timestamp"] is not None else None)
            latest_received_time = (self._timestamp(row["latest_observation_ingested_at"])
                                    if row["latest_observation_ingested_at"] is not None else None)
            row_is_new = (latest_provider_time is None or provider_time > latest_provider_time
                          or (provider_time == latest_provider_time and latest_received_time is not None
                              and received_time > latest_received_time))
            reached = fresh and row_is_new and price is not None and price >= Decimal(row["target_value"])
            if not row_is_new:
                continue
            if reached:
                event = await self.session.execute(text("""
                  insert into public.target_events(session_id,user_id,event_key,event_type,provider_timestamp,
                    observed_price,payload)
                  values (:session_id,:user_id,:event_key,'target_reached',:provider_timestamp,:price,
                    cast(:payload as jsonb))
                  on conflict (event_key) do nothing returning id
                """), {"session_id": row["session_id"], "user_id": row["user_id"],
                       "event_key": f"target_reached:{row['session_id']}:{row['multiplier']}",
                       "provider_timestamp": provider_time, "price": price,
                       "payload": json.dumps({"multiplier": row["multiplier"], "target_value": str(row["target_value"]),
                                              "source_observation": source_observation})})
                if event.scalar_one_or_none() is None:
                    continue
            await self.session.execute(text("""
              update public.target_tracking_levels set
                status=:status,
                first_reached_at=case when :reached then :provider_timestamp else first_reached_at end,
                source_observation=case when :reached then cast(:observation as jsonb) else source_observation end,
                last_observed_at=:provider_timestamp,last_observation=cast(:observation as jsonb),updated_at=now()
              where id=:level_id and first_reached_at is null
            """), {"status": "REACHED" if reached else "NOT_REACHED" if fresh else "UNVERIFIED",
                   "reached": reached, "provider_timestamp": provider_time,
                   "observation": json.dumps(source_observation),
                   "level_id": row["level_id"]})
        if rows:
            await self.session.commit()
        return len(rows)

    async def mark_stale_sessions(self) -> int:
        """Mark active, unreached levels unverified after their last source tick ages out."""
        expired = await self.session.execute(text("""
          update public.target_tracking_sessions t set status='expired',verification_status='unverified',updated_at=now()
          from public.option_expiries e
          where t.option_expiry_id=e.id and t.status='active'
            and (e.expiry_date < (now() at time zone 'Asia/Kolkata')::date
              or (e.expiry_date = (now() at time zone 'Asia/Kolkata')::date
                and (now() at time zone 'Asia/Kolkata')::time >= time '15:30'))
          returning t.id,t.user_id
        """))
        expired_rows = expired.mappings().all()
        for row in expired_rows:
            await self.session.execute(text("""
              update public.target_tracking_levels set status='UNVERIFIED',updated_at=now()
              where session_id=:session_id and user_id=:user_id and first_reached_at is null
            """), {"session_id": row["id"], "user_id": row["user_id"]})
            await self.session.execute(text("""
              insert into public.target_events(session_id,user_id,event_key,event_type,payload)
              values (:session_id,:user_id,:event_key,'session_expired',cast(:payload as jsonb))
              on conflict (event_key) do nothing
            """), {"session_id": row["id"], "user_id": row["user_id"],
                   "event_key": f"session_expired:{row['id']}",
                   "payload": json.dumps({"reason": "contract_expiry_and_regular_market_close"})})
        result = await self.session.execute(text("""
          with stale_levels as (
            update public.target_tracking_levels l set status='UNVERIFIED',updated_at=now()
            from public.target_tracking_sessions t
            where l.session_id=t.id and l.user_id=t.user_id and t.status='active'
              and l.first_reached_at is null and l.status <> 'UNVERIFIED'
              and (l.last_observed_at is null or l.last_observed_at < now()-make_interval(secs => :age))
            returning l.session_id
          ), stale_sessions as (
            update public.target_tracking_sessions t set verification_status='unverified',updated_at=now()
            where t.status='active' and exists (
              select 1 from public.target_tracking_levels l where l.session_id=t.id and l.user_id=t.user_id
                and l.first_reached_at is null and l.status='UNVERIFIED'
                and (l.last_observed_at is null or l.last_observed_at < now()-make_interval(secs => :age))
            )
            returning t.id
          )
          select count(*) from stale_levels
        """), {"age": self.stale_after_seconds})
        changed = result.scalar_one()
        if changed or expired_rows:
            await self.session.commit()
        return changed + len(expired_rows)

    async def get_session(self, *, session_id: UUID, user_id: UUID) -> dict | None:
        expired = await self.session.execute(text("""
          update public.target_tracking_sessions t set status='expired',verification_status='unverified',updated_at=now()
          from public.option_expiries e
          where t.id=:session_id and t.user_id=:user_id and t.option_expiry_id=e.id and t.status='active'
            and (e.expiry_date < (now() at time zone 'Asia/Kolkata')::date
              or (e.expiry_date = (now() at time zone 'Asia/Kolkata')::date
                and (now() at time zone 'Asia/Kolkata')::time >= time '15:30'))
          returning t.id
        """), {"session_id": session_id, "user_id": user_id})
        if expired.scalar_one_or_none() is not None:
            await self.session.execute(text("""
              update public.target_tracking_levels set status='UNVERIFIED',updated_at=now()
              where session_id=:session_id and user_id=:user_id and first_reached_at is null
            """), {"session_id": session_id, "user_id": user_id})
            await self.session.execute(text("""
              insert into public.target_events(session_id,user_id,event_key,event_type,payload)
              values (:session_id,:user_id,:event_key,'session_expired',cast(:payload as jsonb))
              on conflict (event_key) do nothing
            """), {"session_id": session_id, "user_id": user_id,
                   "event_key": f"session_expired:{session_id}",
                   "payload": json.dumps({"reason": "contract_expiry_and_regular_market_close"})})
            await self.session.commit()
        result = await self.session.execute(text("""
          select t.id,t.status,t.reference_premium,t.reference_timestamp,t.source_observation,
                 t.verification_status,t.instrument_id,t.option_contract_id,i.display_name instrument,
                 t.latest_observed_premium,t.latest_observation_timestamp,t.latest_observation_ingested_at,
                 t.current_multiple,t.created_at,t.updated_at,
                 u.root_symbol underlying,e.expiry_date expiry,c.strike,c.option_type,
                 i.exchange_segment,i.security_id
          from public.target_tracking_sessions t
          join public.option_contracts c on c.id=t.option_contract_id
          join public.instruments i on i.id=c.instrument_id
          join public.underlying_assets u on u.id=c.underlying_id
          join public.option_expiries e on e.id=c.option_expiry_id
          where t.id=:session_id and t.user_id=:user_id
        """), {"session_id": session_id, "user_id": user_id})
        session_row = result.mappings().one_or_none()
        if session_row is None:
            return None
        verification_status = session_row["verification_status"]
        if session_row["status"] == "active":
            quote = await self._read_quote(session_row["exchange_segment"], str(session_row["security_id"]))
            if (quote is None or quote.get("exchange_segment") != session_row["exchange_segment"]
                    or str(quote.get("security_id")) != str(session_row["security_id"])
                    or self._freshness(quote) != "FRESH"):
                verification_status = "unverified"
                await self.session.execute(text("""
                  update public.target_tracking_sessions set verification_status='unverified',updated_at=now()
                  where id=:id and user_id=:user_id and status='active' and verification_status <> 'unverified'
                """), {"id": session_id, "user_id": user_id})
                await self.session.execute(text("""
                  update public.target_tracking_levels set status='UNVERIFIED',updated_at=now()
                  where session_id=:id and user_id=:user_id and first_reached_at is null and status <> 'UNVERIFIED'
                """), {"id": session_id, "user_id": user_id})
            else:
                # Reading a Redis cache does not constitute a new provider
                # observation. Only the background consumer can verify/update.
                verification_status = session_row["verification_status"]
            await self.session.commit()
            refreshed = await self.session.execute(text("""
              select latest_observed_premium,latest_observation_timestamp,latest_observation_ingested_at,
                     current_multiple,verification_status,status
              from public.target_tracking_sessions where id=:id and user_id=:user_id
            """), {"id": session_id, "user_id": user_id})
            session_row = {**dict(session_row), **dict(refreshed.mappings().one())}
        levels_result = await self.session.execute(text("""
          select multiplier,target_value,status,first_reached_at,source_observation,
                 last_observed_at,last_observation
          from public.target_tracking_levels where session_id=:id and user_id=:user_id
          order by multiplier
        """), {"id": session_id, "user_id": user_id})
        return {**dict(session_row), "option_type": str(session_row["option_type"]).upper(),
                "session_id": str(session_row["id"]),
                "verification_status": verification_status if session_row["status"] == "active" else session_row["verification_status"],
                "data_quality": "LIVE" if session_row["verification_status"] == "verified" else "DATA STALE",
                "targets": [dict(row) for row in levels_result.mappings()],
                "message": "Targets are tracking levels, not predictions or guarantees."}

    async def list_sessions(self, user_id: UUID) -> list[dict]:
        rows = await self.session.execute(text("""
          select id from public.target_tracking_sessions where user_id=:user_id
          order by created_at desc limit 200
        """), {"user_id": user_id})
        sessions = []
        for row in rows:
            item = await self.get_session(session_id=row[0], user_id=user_id)
            if item is not None:
                sessions.append(item)
        return sessions

    async def cancel(self, *, session_id: UUID, user_id: UUID) -> bool:
        result = await self.session.execute(text("""
          update public.target_tracking_sessions set status='canceled',updated_at=now()
          where id=:id and user_id=:user_id and status='active' returning id
        """), {"id": session_id, "user_id": user_id})
        await self.session.commit()
        return result.scalar_one_or_none() is not None

    async def _read_quote(self, segment: str, security_id: str) -> dict | None:
        try:
            raw = await self.redis.get(f"market:quote:{segment}:{security_id}")
            return json.loads(raw) if raw else None
        except Exception:
            return None

    def _freshness(self, observation: dict) -> str:
        if observation.get("source") != "DHANHQ":
            return "UNVERIFIED"
        try:
            provider_at = self._timestamp(observation.get("exchange_timestamp"))
            received_at = self._timestamp(observation.get("received_at") or observation.get("ingested_at"))
            TargetCalculationService.validate_reference_premium(observation.get("last_price"))
        except (TypeError, ValueError):
            return "UNVERIFIED"
        now = datetime.now(timezone.utc)
        provider_age = (now - provider_at).total_seconds()
        received_age = (now - received_at).total_seconds()
        if min(provider_age, received_age) < -2:
            return "UNVERIFIED"
        if max(provider_age, received_age) > self.stale_after_seconds:
            return "UNVERIFIED"
        return "FRESH"

    @staticmethod
    def _timestamp(value) -> datetime:
        if not isinstance(value, str):
            raise ValueError("Dhan timestamp is unavailable")
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if result.tzinfo is None:
            raise ValueError("Dhan timestamp must include a timezone")
        return result.astimezone(timezone.utc)


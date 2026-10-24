"""Notification channel abstraction with a durable Supabase-backed inbox."""
from __future__ import annotations

import json
from datetime import datetime
from typing import Protocol

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


class NotificationChannel(Protocol):
    async def deliver_pending(self, session: AsyncSession, redis) -> int: ...


class InAppNotificationChannel:
    """Write the user inbox first, then publish a small user-scoped socket event."""

    async def deliver_pending(self, session: AsyncSession, redis, observation: dict) -> int:
        # target_events are committed by TargetStatusService before this outbox
        # transaction begins. The unique event key makes recovery idempotent.
        await session.execute(text("""
          insert into public.user_notifications
            (user_id,target_event_id,event_key,notification_type,title,body,payload)
          select e.user_id,e.id,e.event_key,'target_reached',
            concat('Target ', coalesce(e.payload->>'multiplier',''), ' reached'),
            concat(coalesce(u.root_symbol,'Option'), ' ', coalesce(s.strike::text,'—'), ' ',
              upper(coalesce(s.option_type,'')), ' premium reached ₹', coalesce(e.observed_price::text,'—')),
            jsonb_build_object('event_id',e.id,'event_key',e.event_key,'session_id',e.session_id,
              'provider_timestamp',e.provider_timestamp,'observed_price',e.observed_price,
              'multiplier',e.payload->>'multiplier','target_value',e.payload->>'target_value',
              'source_observation',e.payload->'source_observation')
          from public.target_events e
          join public.target_tracking_sessions s on s.id=e.session_id and s.user_id=e.user_id
          left join public.underlying_assets u on u.id=s.underlying_id
          where e.event_type='target_reached'
            and e.provider_timestamp=:provider_timestamp
            and e.payload #>> '{source_observation,security_id}'=:security_id
            and e.payload #>> '{source_observation,exchange_segment}'=:exchange_segment
          on conflict (user_id,event_key) do nothing
        """), {
            "provider_timestamp": datetime.fromisoformat(
                observation["exchange_timestamp"].replace("Z", "+00:00")),
            "security_id": str(observation["security_id"]),
            "exchange_segment": observation["exchange_segment"],
        })
        await session.commit()

        rows = await session.execute(text("""
          select id,user_id,event_key,notification_type,title,body,payload
          from public.user_notifications where realtime_published_at is null
          order by created_at limit 100
          for update skip locked
        """))
        pending = rows.mappings().all()
        delivered = 0
        for row in pending:
            channel = f"market-pulse:alerts:{row['user_id']}"
            # The inbox row is already committed. A publish retry may send a
            # duplicate frame; clients deduplicate by notification id.
            await redis.publish(channel, json.dumps({
                "type": "target_alert", "notification_id": str(row["id"]),
                "event_key": row["event_key"], "notification_type": row["notification_type"],
                "title": row["title"], "body": row["body"], "payload": row["payload"],
            }, default=str, separators=(",", ":")))
            await session.execute(text("""
              update public.user_notifications set realtime_published_at=now(),updated_at=now()
              where id=:id and realtime_published_at is null
            """), {"id": row["id"]})
            delivered += 1
        if delivered:
            await session.commit()
        return delivered


class AlertEngine:
    """Orchestrates channels; external channels can be registered without blocking Phase 1."""

    def __init__(self, channels: list[NotificationChannel] | None = None):
        self.channels = channels or [InAppNotificationChannel()]

    async def deliver_pending(self, session: AsyncSession, redis, observation: dict) -> int:
        delivered = 0
        for channel in self.channels:
            delivered += await channel.deliver_pending(session, redis, observation)
        return delivered

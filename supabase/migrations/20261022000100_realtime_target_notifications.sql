-- Durable per-user inbox/outbox for target events. The source event is committed
-- before this notification is published to the authenticated user's socket.
create unique index if not exists target_events_id_user_unique
  on public.target_events(id,user_id);

create table if not exists public.user_notifications (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references auth.users(id) on delete cascade,
  target_event_id uuid not null,
  event_key text not null,
  notification_type text not null check (notification_type in ('target_reached')),
  title text not null,
  body text not null,
  payload jsonb not null default '{}'::jsonb,
  read_at timestamptz,
  realtime_published_at timestamptz,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  constraint user_notifications_event_channel_unique unique (user_id, event_key),
  constraint user_notifications_owner_event_unique unique (user_id, target_event_id),
  constraint user_notifications_owner_target_event_fk foreign key (target_event_id,user_id)
    references public.target_events(id,user_id) on delete cascade
);

create index if not exists user_notifications_owner_created_idx
  on public.user_notifications(user_id, created_at desc);
create index if not exists user_notifications_owner_unread_idx
  on public.user_notifications(user_id, created_at desc) where read_at is null;
create index if not exists user_notifications_realtime_pending_idx
  on public.user_notifications(created_at) where realtime_published_at is null;

drop trigger if exists user_notifications_set_updated_at on public.user_notifications;
create trigger user_notifications_set_updated_at before update on public.user_notifications
  for each row execute function public.set_updated_at();

alter table public.user_notifications enable row level security;
drop policy if exists user_notifications_owner_read on public.user_notifications;
create policy user_notifications_owner_read on public.user_notifications
  for select to authenticated using (user_id = (select auth.uid()));
drop policy if exists user_notifications_owner_read_update on public.user_notifications;
create policy user_notifications_owner_read_update on public.user_notifications
  for update to authenticated using (user_id = (select auth.uid()))
  with check (user_id = (select auth.uid()));

revoke all on public.user_notifications from anon, authenticated;
grant select on public.user_notifications to authenticated;
grant update (read_at) on public.user_notifications to authenticated;
grant all on public.user_notifications to service_role;


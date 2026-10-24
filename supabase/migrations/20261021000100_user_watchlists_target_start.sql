-- Persist the latest live target observation separately from the immutable baseline.
alter table public.target_tracking_sessions
  add column if not exists latest_observed_premium numeric(24,8),
  add column if not exists latest_observation_timestamp timestamptz,
  add column if not exists latest_observation_ingested_at timestamptz,
  add column if not exists current_multiple numeric(24,8),
  add column if not exists start_request_key uuid,
  add constraint target_latest_premium_positive check (latest_observed_premium is null or latest_observed_premium > 0),
  add constraint target_current_multiple_positive check (current_multiple is null or current_multiple > 0);

create unique index if not exists target_sessions_start_request_unique
  on public.target_tracking_sessions(user_id,start_request_key)
  where start_request_key is not null;

-- One quote may cross multiple premium levels for one session. The older session/type/time
-- key incorrectly prevented recording those distinct multiplier events in the same tick.
drop index if exists public.target_events_session_time_unique;

comment on column public.target_tracking_sessions.latest_observed_premium is
  'Latest positive Dhan premium; updated only from the exact mapped live feed instrument.';
comment on column public.target_tracking_sessions.latest_observation_timestamp is
  'Original exchange timestamp of latest_observed_premium, UTC instant.';
comment on column public.target_tracking_sessions.latest_observation_ingested_at is
  'Backend receive timestamp for latest_observed_premium, kept separate from exchange time.';
comment on column public.target_tracking_sessions.current_multiple is
  'Latest observed premium divided by the immutable reference premium; null when no observation exists.';
comment on column public.target_tracking_sessions.start_request_key is
  'Client-generated idempotency UUID for explicit two-step target start confirmation.';

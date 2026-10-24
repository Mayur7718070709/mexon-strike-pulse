-- Dhan rolling expired-options history is not a live chain snapshot.
-- The endpoint supplies expiryFlag/expiryCode, not a calendar expiry date.
alter table public.expired_option_observations
  alter column option_expiry_id drop not null,
  add column if not exists expiry_flag text,
  add column if not exists expiry_code smallint,
  add column if not exists strike_selection text,
  add column if not exists interval text,
  add column if not exists source text,
  add column if not exists underlying_security_id text,
  add column if not exists exchange_segment text,
  add column if not exists open_price numeric(24,8),
  add column if not exists high_price numeric(24,8),
  add column if not exists low_price numeric(24,8),
  add column if not exists close_price numeric(24,8),
  add column if not exists spot_price numeric(24,8);

alter table public.expired_option_observations
  add constraint expired_obs_expiry_flag_check check (expiry_flag is null or expiry_flag in ('WEEK','MONTH')),
  add constraint expired_obs_expiry_code_check check (expiry_code is null or expiry_code in (0,1,2)),
  add constraint expired_obs_interval_check check (interval is null or interval in ('1','5','15','25','60')),
  add constraint expired_obs_source_check check (source is null or source='DHANHQ'),
  add constraint expired_obs_ohlc_check check (
    (open_price is null or open_price >= 0) and (high_price is null or high_price >= 0) and
    (low_price is null or low_price >= 0) and (close_price is null or close_price >= 0) and
    (spot_price is null or spot_price > 0) and
    (high_price is null or low_price is null or high_price >= low_price)
  );

create unique index if not exists expired_obs_rolling_idempotency_unique
  on public.expired_option_observations(provider_id,underlying_id,observation_kind,strike,option_type,provider_timestamp)
  where source='DHANHQ' and expiry_flag is not null and expiry_code is not null
    and strike_selection is not null and interval is not null;
create index if not exists expired_obs_rolling_coverage_idx
  on public.expired_option_observations(underlying_id,expiry_flag,expiry_code,interval,provider_timestamp desc);
create index if not exists expired_obs_rolling_strike_idx
  on public.expired_option_observations(underlying_id,strike,option_type,provider_timestamp desc);

create table if not exists public.expired_options_ingestion_jobs (
  id uuid primary key default gen_random_uuid(),
  requested_by uuid not null references auth.users(id) on delete cascade,
  provider_id uuid not null references public.market_data_providers(id) on delete restrict,
  underlying_id uuid not null references public.underlying_assets(id) on delete restrict,
  instrument_id uuid not null references public.instruments(id) on delete restrict,
  exchange_segment text not null check (exchange_segment='NSE_FNO'),
  security_id text not null,
  instrument text not null check (instrument in ('OPTIDX','OPTSTK')),
  interval text not null check (interval in ('1','5','15','25','60')),
  expiry_flag text not null check (expiry_flag in ('WEEK','MONTH')),
  expiry_code smallint not null check (expiry_code in (0,1,2)),
  strike_selections jsonb not null,
  option_types jsonb not null,
  range_start timestamptz not null,
  range_end timestamptz not null,
  idempotency_key text not null,
  status text not null default 'queued' check (status in ('queued','running','succeeded','partial','failed')),
  sample_verified boolean not null default false,
  chunks_total integer not null default 0 check (chunks_total >= 0),
  chunks_completed integer not null default 0 check (chunks_completed >= 0),
  chunks_failed integer not null default 0 check (chunks_failed >= 0),
  rows_received bigint not null default 0 check (rows_received >= 0),
  rows_upserted bigint not null default 0 check (rows_upserted >= 0),
  attempt_count integer not null default 0 check (attempt_count >= 0),
  error_code text,
  error_message text,
  coverage_report jsonb not null default '{}'::jsonb,
  metadata jsonb not null default '{}'::jsonb,
  started_at timestamptz,
  completed_at timestamptz,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  constraint expired_options_job_range_check check (range_end > range_start),
  constraint expired_options_job_user_key_unique unique (requested_by,idempotency_key)
);

create table if not exists public.expired_options_ingestion_chunks (
  id uuid primary key default gen_random_uuid(),
  job_id uuid not null references public.expired_options_ingestion_jobs(id) on delete cascade,
  range_start timestamptz not null,
  range_end timestamptz not null,
  strike_selection text not null,
  option_type text not null check (option_type in ('CALL','PUT')),
  is_sample boolean not null default false,
  status text not null default 'queued' check (status in ('queued','running','succeeded','partial','failed')),
  attempt_count integer not null default 0 check (attempt_count >= 0),
  rows_received bigint not null default 0 check (rows_received >= 0),
  rows_upserted bigint not null default 0 check (rows_upserted >= 0),
  invalid_rows bigint not null default 0 check (invalid_rows >= 0),
  provider_first_timestamp timestamptz,
  provider_last_timestamp timestamptz,
  error_code text,
  error_message text,
  started_at timestamptz,
  completed_at timestamptz,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  constraint expired_options_chunk_range_check check (range_end > range_start),
  constraint expired_options_chunk_range_cap check (range_end <= range_start + interval '30 days'),
  constraint expired_options_chunk_unique unique (job_id,range_start,range_end,strike_selection,option_type)
);

create index if not exists expired_options_jobs_admin_status_idx
  on public.expired_options_ingestion_jobs(status,created_at desc);
create index if not exists expired_options_jobs_owner_idx
  on public.expired_options_ingestion_jobs(requested_by,created_at desc);
create index if not exists expired_options_chunks_claim_idx
  on public.expired_options_ingestion_chunks(job_id,status,range_start);

drop trigger if exists set_updated_at on public.expired_options_ingestion_jobs;
create trigger set_updated_at before update on public.expired_options_ingestion_jobs
  for each row execute function public.set_updated_at();
drop trigger if exists set_updated_at on public.expired_options_ingestion_chunks;
create trigger set_updated_at before update on public.expired_options_ingestion_chunks
  for each row execute function public.set_updated_at();

alter table public.expired_options_ingestion_jobs enable row level security;
alter table public.expired_options_ingestion_chunks enable row level security;
drop policy if exists expired_options_jobs_read_owner on public.expired_options_ingestion_jobs;
create policy expired_options_jobs_read_owner on public.expired_options_ingestion_jobs
  for select to authenticated using (requested_by=(select auth.uid()));
drop policy if exists expired_options_chunks_read_owner on public.expired_options_ingestion_chunks;
create policy expired_options_chunks_read_owner on public.expired_options_ingestion_chunks
  for select to authenticated using (exists (
    select 1 from public.expired_options_ingestion_jobs j
    where j.id=job_id and j.requested_by=(select auth.uid())
  ));

grant select on public.expired_options_ingestion_jobs,public.expired_options_ingestion_chunks to authenticated;
grant all on public.expired_options_ingestion_jobs,public.expired_options_ingestion_chunks to service_role;
grant all on public.expired_option_observations to service_role;

comment on column public.expired_option_observations.option_expiry_id is
  'Nullable for rolling Dhan responses: the endpoint returns relative expiryFlag/expiryCode and does not identify a calendar expiry date.';
comment on column public.expired_option_observations.expiry_flag is 'Dhan rolling-option request field: WEEK or MONTH.';
comment on column public.expired_option_observations.expiry_code is 'Dhan rolling-option request field: documented enum 0, 1 or 2; not a calendar expiry date.';

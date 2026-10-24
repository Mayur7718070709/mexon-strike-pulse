-- Durable, restartable provider-backed historical ingestion.
alter table public.historical_ingestion_jobs
  add column if not exists sample_verified boolean not null default false,
  add column if not exists chunks_total integer not null default 0,
  add column if not exists chunks_completed integer not null default 0,
  add column if not exists chunks_failed integer not null default 0,
  add column if not exists successful_ranges jsonb not null default '[]'::jsonb,
  add column if not exists coverage_report jsonb not null default '{}'::jsonb;

create table if not exists public.historical_ingestion_chunks (
  id uuid primary key default gen_random_uuid(),
  job_id uuid not null references public.historical_ingestion_jobs(id) on delete cascade,
  range_start timestamptz not null,
  range_end timestamptz not null,
  is_sample boolean not null default false,
  status text not null default 'queued' check (status in ('queued','running','succeeded','partial','failed')),
  attempt_count integer not null default 0 check (attempt_count >= 0),
  rows_received bigint not null default 0 check (rows_received >= 0),
  rows_upserted bigint not null default 0 check (rows_upserted >= 0),
  invalid_rows bigint not null default 0 check (invalid_rows >= 0),
  duplicate_rows bigint not null default 0 check (duplicate_rows >= 0),
  provider_first_timestamp timestamptz,
  provider_last_timestamp timestamptz,
  error_code text,
  error_message text,
  started_at timestamptz,
  completed_at timestamptz,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  constraint historical_chunk_range_check check (range_end > range_start),
  constraint historical_chunk_job_range_unique unique (job_id, range_start, range_end)
);

create index if not exists historical_chunks_claim_idx
  on public.historical_ingestion_chunks(job_id, status, range_start);
create index if not exists historical_jobs_admin_idx
  on public.historical_ingestion_jobs(status, created_at desc);

alter table public.historical_ingestion_chunks enable row level security;
drop policy if exists historical_chunks_read_job_owner on public.historical_ingestion_chunks;
create policy historical_chunks_read_job_owner on public.historical_ingestion_chunks
  for select to authenticated using (exists (
    select 1 from public.historical_ingestion_jobs j
    where j.id = job_id and j.requested_by = (select auth.uid())
  ));
grant select on public.historical_ingestion_chunks to authenticated;
grant all on public.historical_ingestion_chunks to service_role;

comment on column public.historical_ingestion_jobs.sample_verified is 'True only after a non-empty, valid response from the configured Dhan historical endpoint.';
comment on column public.historical_ingestion_jobs.successful_ranges is 'Provider-returned chunk ranges successfully persisted; not an assertion of continuous coverage.';

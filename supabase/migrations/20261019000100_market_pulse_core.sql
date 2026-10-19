-- Market Pulse core schema for Supabase PostgreSQL. No market rows are seeded.
-- All timestamps use timestamptz (absolute UTC instants). Provider and ingestion
-- timestamps are separate so delayed data is never represented as current.

create extension if not exists pgcrypto with schema extensions;

create or replace function public.set_updated_at()
returns trigger language plpgsql set search_path = public, pg_temp as $$
begin new.updated_at := now(); return new; end;
$$;

create table if not exists public.profiles (
  id uuid primary key references auth.users(id) on delete cascade,
  email text, display_name text, avatar_url text,
  created_at timestamptz not null default now(), updated_at timestamptz not null default now()
);

create table if not exists public.subscription_plans (
  id uuid primary key default gen_random_uuid(), code text not null unique,
  name text not null, description text,
  billing_interval text not null check (billing_interval in ('month','year','one_time')),
  price_minor bigint not null check (price_minor >= 0), currency char(3) not null default 'INR',
  entitlements jsonb not null default '{}'::jsonb, is_active boolean not null default true,
  display_order integer not null default 0,
  created_at timestamptz not null default now(), updated_at timestamptz not null default now()
);

create table if not exists public.subscriptions (
  id uuid primary key default gen_random_uuid(), user_id uuid not null references auth.users(id) on delete cascade,
  plan_id uuid not null references public.subscription_plans(id) on delete restrict,
  billing_provider text not null, provider_customer_id text, provider_subscription_id text,
  status text not null check (status in ('incomplete','trialing','active','past_due','paused','canceled','unpaid','expired')),
  current_period_start timestamptz, current_period_end timestamptz,
  cancel_at_period_end boolean not null default false, canceled_at timestamptz,
  metadata jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now(), updated_at timestamptz not null default now(),
  constraint subscriptions_provider_id_unique unique (billing_provider, provider_subscription_id),
  constraint subscriptions_period_order check (current_period_end is null or current_period_start is null or current_period_end > current_period_start)
);

create table if not exists public.market_data_providers (
  id uuid primary key default gen_random_uuid(), code text not null unique, display_name text not null,
  provider_type text not null check (provider_type in ('market_data','billing','notification')),
  is_enabled boolean not null default false, configuration jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now(), updated_at timestamptz not null default now()
);
comment on column public.market_data_providers.configuration is
  'Non-secret provider metadata only. Store API tokens and credentials in backend secret management.';

create table if not exists public.instruments (
  id uuid primary key default gen_random_uuid(),
  provider_id uuid not null references public.market_data_providers(id) on delete restrict,
  exchange_segment text not null, security_id text not null, symbol text not null, display_name text not null,
  asset_type text not null check (asset_type in ('index','equity','future','option','currency','commodity','other')),
  currency char(3) not null default 'INR', tick_size numeric(20,8), lot_size integer,
  is_active boolean not null default true, metadata jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now(), updated_at timestamptz not null default now(),
  constraint instruments_provider_security_unique unique (provider_id, exchange_segment, security_id),
  constraint instruments_id_provider_unique unique (id, provider_id),
  constraint instruments_tick_size_positive check (tick_size is null or tick_size > 0),
  constraint instruments_lot_size_positive check (lot_size is null or lot_size > 0)
);

create table if not exists public.underlying_assets (
  id uuid primary key default gen_random_uuid(), instrument_id uuid not null unique references public.instruments(id) on delete restrict,
  underlying_type text not null check (underlying_type in ('index','equity','commodity','currency','other')),
  root_symbol text not null, currency char(3) not null default 'INR', metadata jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now(), updated_at timestamptz not null default now(),
  constraint underlying_assets_id_instrument_unique unique (id, instrument_id)
);

create table if not exists public.option_expiries (
  id uuid primary key default gen_random_uuid(), underlying_id uuid not null references public.underlying_assets(id) on delete restrict,
  expiry_date date not null, expiry_kind text not null default 'regular' check (expiry_kind in ('regular','weekly','monthly','quarterly','other')),
  is_active boolean not null default true,
  created_at timestamptz not null default now(), updated_at timestamptz not null default now(),
  constraint option_expiries_underlying_date_kind_unique unique (underlying_id, expiry_date, expiry_kind),
  constraint option_expiries_id_underlying_unique unique (id, underlying_id)
);

create table if not exists public.option_contracts (
  id uuid primary key default gen_random_uuid(), instrument_id uuid not null unique references public.instruments(id) on delete restrict,
  underlying_id uuid not null references public.underlying_assets(id) on delete restrict,
  option_expiry_id uuid not null, strike numeric(24,8) not null check (strike >= 0),
  option_type text not null check (option_type in ('call','put')), contract_symbol text not null,
  lot_size integer not null check (lot_size > 0), contract_multiplier numeric(20,8) not null default 1 check (contract_multiplier > 0),
  created_at timestamptz not null default now(), updated_at timestamptz not null default now(),
  constraint option_contracts_expiry_underlying_fk foreign key (option_expiry_id, underlying_id)
    references public.option_expiries(id, underlying_id) on delete restrict,
  constraint option_contracts_contract_unique unique (underlying_id, option_expiry_id, strike, option_type),
  constraint option_contracts_snapshot_identity_unique unique (id, underlying_id, option_expiry_id, strike, option_type)
);

-- Live tick/quote snapshots are not historical candles.
create table if not exists public.live_market_snapshots (
  id uuid primary key default gen_random_uuid(), provider_id uuid not null references public.market_data_providers(id) on delete restrict,
  instrument_id uuid not null references public.instruments(id) on delete restrict,
  provider_timestamp timestamptz not null, ingested_at timestamptz not null default now(),
  packet_type text not null check (packet_type in ('ticker','quote','full','depth')),
  last_price numeric(24,8), last_quantity bigint, volume bigint, open_interest bigint,
  average_price numeric(24,8), open_price numeric(24,8), high_price numeric(24,8), low_price numeric(24,8), close_price numeric(24,8),
  bid_price numeric(24,8), bid_quantity bigint, ask_price numeric(24,8), ask_quantity bigint,
  raw_payload jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now(), updated_at timestamptz not null default now(),
  constraint live_snapshot_instrument_provider_fk foreign key (instrument_id, provider_id)
    references public.instruments(id, provider_id) on delete restrict,
  constraint live_snapshots_counts_nonnegative check (
    (last_quantity is null or last_quantity >= 0) and (volume is null or volume >= 0) and
    (open_interest is null or open_interest >= 0) and (bid_quantity is null or bid_quantity >= 0) and (ask_quantity is null or ask_quantity >= 0))
);

create table if not exists public.option_chain_snapshots (
  id uuid primary key default gen_random_uuid(), provider_id uuid not null references public.market_data_providers(id) on delete restrict,
  underlying_id uuid not null references public.underlying_assets(id) on delete restrict,
  option_expiry_id uuid not null, option_contract_id uuid not null, strike numeric(24,8) not null,
  option_type text not null check (option_type in ('call','put')),
  provider_timestamp timestamptz not null, ingested_at timestamptz not null default now(),
  last_price numeric(24,8), bid_price numeric(24,8), ask_price numeric(24,8), volume bigint, open_interest bigint,
  implied_volatility numeric(18,10), delta numeric(18,10), gamma numeric(18,10), theta numeric(18,10), vega numeric(18,10),
  raw_payload jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now(), updated_at timestamptz not null default now(),
  constraint option_chain_expiry_underlying_fk foreign key (option_expiry_id, underlying_id)
    references public.option_expiries(id, underlying_id) on delete restrict,
  constraint option_chain_contract_identity_fk foreign key (option_contract_id, underlying_id, option_expiry_id, strike, option_type)
    references public.option_contracts(id, underlying_id, option_expiry_id, strike, option_type) on delete restrict,
  constraint option_chain_counts_nonnegative check ((volume is null or volume >= 0) and (open_interest is null or open_interest >= 0))
);

create table if not exists public.historical_candles (
  id uuid primary key default gen_random_uuid(), provider_id uuid not null references public.market_data_providers(id) on delete restrict,
  instrument_id uuid not null references public.instruments(id) on delete restrict,
  timeframe text not null check (timeframe in ('1m','2m','3m','5m','10m','15m','30m','60m','1d','1w','1mo')),
  candle_timestamp timestamptz not null, provider_timestamp timestamptz not null, ingested_at timestamptz not null default now(),
  open_price numeric(24,8) not null, high_price numeric(24,8) not null, low_price numeric(24,8) not null, close_price numeric(24,8) not null,
  volume bigint, open_interest bigint,
  created_at timestamptz not null default now(), updated_at timestamptz not null default now(),
  constraint historical_candle_instrument_provider_fk foreign key (instrument_id, provider_id)
    references public.instruments(id, provider_id) on delete restrict,
  constraint historical_candles_idempotency_unique unique (provider_id, instrument_id, timeframe, candle_timestamp),
  constraint historical_candles_ohlc_check check (high_price >= low_price and high_price >= open_price and high_price >= close_price and low_price <= open_price and low_price <= close_price),
  constraint historical_candles_counts_nonnegative check ((volume is null or volume >= 0) and (open_interest is null or open_interest >= 0))
);

create table if not exists public.expired_option_observations (
  id uuid primary key default gen_random_uuid(), provider_id uuid not null references public.market_data_providers(id) on delete restrict,
  underlying_id uuid not null references public.underlying_assets(id) on delete restrict,
  option_expiry_id uuid not null, option_contract_id uuid, strike numeric(24,8) not null,
  option_type text not null check (option_type in ('call','put')), observation_kind text not null,
  provider_timestamp timestamptz not null, ingested_at timestamptz not null default now(),
  last_price numeric(24,8), volume bigint, open_interest bigint, implied_volatility numeric(18,10),
  observation jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now(), updated_at timestamptz not null default now(),
  constraint expired_obs_expiry_underlying_fk foreign key (option_expiry_id, underlying_id)
    references public.option_expiries(id, underlying_id) on delete restrict,
  constraint expired_obs_contract_identity_fk foreign key (option_contract_id, underlying_id, option_expiry_id, strike, option_type)
    references public.option_contracts(id, underlying_id, option_expiry_id, strike, option_type) on delete restrict,
  constraint expired_obs_counts_nonnegative check ((volume is null or volume >= 0) and (open_interest is null or open_interest >= 0))
);

create table if not exists public.historical_ingestion_jobs (
  id uuid primary key default gen_random_uuid(), requested_by uuid references auth.users(id) on delete set null,
  provider_id uuid not null references public.market_data_providers(id) on delete restrict,
  instrument_id uuid not null references public.instruments(id) on delete restrict,
  timeframe text not null check (timeframe in ('1m','2m','3m','5m','10m','15m','30m','60m','1d','1w','1mo')),
  range_start timestamptz not null, range_end timestamptz not null, idempotency_key text not null unique,
  status text not null default 'queued' check (status in ('queued','running','succeeded','partial','failed','canceled')),
  attempt_count integer not null default 0 check (attempt_count >= 0), available_at timestamptz not null default now(),
  started_at timestamptz, completed_at timestamptz, rows_received bigint not null default 0 check (rows_received >= 0),
  rows_upserted bigint not null default 0 check (rows_upserted >= 0), error_code text, error_message text,
  metadata jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now(), updated_at timestamptz not null default now(),
  constraint ingestion_job_instrument_provider_fk foreign key (instrument_id, provider_id)
    references public.instruments(id, provider_id) on delete restrict,
  constraint ingestion_jobs_range_check check (range_end > range_start)
);

create table if not exists public.watchlists (
  id uuid primary key default gen_random_uuid(), user_id uuid not null references auth.users(id) on delete cascade,
  name text not null, description text, is_default boolean not null default false,
  created_at timestamptz not null default now(), updated_at timestamptz not null default now(),
  constraint watchlists_user_name_unique unique (user_id, name),
  constraint watchlists_id_user_unique unique (id, user_id)
);

create table if not exists public.watchlist_items (
  id uuid primary key default gen_random_uuid(), user_id uuid not null references auth.users(id) on delete cascade,
  watchlist_id uuid not null, instrument_id uuid references public.instruments(id) on delete restrict,
  option_contract_id uuid references public.option_contracts(id) on delete restrict,
  display_order integer not null default 0, note text,
  created_at timestamptz not null default now(), updated_at timestamptz not null default now(),
  constraint watchlist_items_owner_fk foreign key (watchlist_id, user_id) references public.watchlists(id, user_id) on delete cascade,
  constraint watchlist_items_exactly_one_subject check (num_nonnulls(instrument_id, option_contract_id) = 1)
);

create table if not exists public.target_tracking_sessions (
  id uuid primary key default gen_random_uuid(), user_id uuid not null references auth.users(id) on delete cascade,
  instrument_id uuid references public.instruments(id) on delete restrict,
  option_contract_id uuid references public.option_contracts(id) on delete restrict,
  direction text not null check (direction in ('above','below','crosses_above','crosses_below')),
  target_price numeric(24,8) not null,
  status text not null default 'active' check (status in ('active','triggered','paused','completed','canceled','expired')),
  starts_at timestamptz not null default now(), expires_at timestamptz, triggered_at timestamptz,
  metadata jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now(), updated_at timestamptz not null default now(),
  constraint target_sessions_exactly_one_subject check (num_nonnulls(instrument_id, option_contract_id) = 1),
  constraint target_sessions_expiry_after_start check (expires_at is null or expires_at > starts_at),
  constraint target_sessions_id_user_unique unique (id, user_id)
);

create table if not exists public.target_events (
  id uuid primary key default gen_random_uuid(), session_id uuid not null, user_id uuid not null references auth.users(id) on delete cascade,
  event_key text not null unique,
  event_type text not null check (event_type in ('target_reached','target_crossed','session_expired','session_canceled')),
  provider_timestamp timestamptz, ingested_at timestamptz not null default now(),
  observed_price numeric(24,8), payload jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now(), updated_at timestamptz not null default now(),
  constraint target_events_owner_session_fk foreign key (session_id, user_id)
    references public.target_tracking_sessions(id, user_id) on delete cascade
);

create table if not exists public.scanner_results (
  id uuid primary key default gen_random_uuid(), user_id uuid references auth.users(id) on delete cascade,
  scanner_code text not null, instrument_id uuid references public.instruments(id) on delete restrict,
  underlying_id uuid references public.underlying_assets(id) on delete restrict,
  option_contract_id uuid references public.option_contracts(id) on delete restrict,
  provider_timestamp timestamptz not null, ingested_at timestamptz not null default now(),
  result_key text not null, score numeric(12,6), result jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now(), updated_at timestamptz not null default now()
);

create table if not exists public.opportunity_scores (
  id uuid primary key default gen_random_uuid(), user_id uuid references auth.users(id) on delete cascade,
  instrument_id uuid references public.instruments(id) on delete restrict,
  underlying_id uuid references public.underlying_assets(id) on delete restrict,
  option_contract_id uuid references public.option_contracts(id) on delete restrict,
  score_type text not null, score numeric(12,6) not null, model_version text not null,
  provider_timestamp timestamptz not null, ingested_at timestamptz not null default now(),
  components jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now(), updated_at timestamptz not null default now(),
  constraint opportunity_scores_subject_required check (num_nonnulls(instrument_id, underlying_id, option_contract_id) >= 1)
);

create table if not exists public.alerts (
  id uuid primary key default gen_random_uuid(), user_id uuid not null references auth.users(id) on delete cascade,
  name text not null, alert_type text not null check (alert_type in ('price','target','scanner','option','system')),
  instrument_id uuid references public.instruments(id) on delete restrict,
  option_contract_id uuid references public.option_contracts(id) on delete restrict,
  condition jsonb not null, channels text[] not null default array['in_app']::text[],
  status text not null default 'active' check (status in ('active','paused','triggered','completed','canceled')),
  last_triggered_at timestamptz,
  created_at timestamptz not null default now(), updated_at timestamptz not null default now(),
  constraint alerts_exactly_one_subject check (num_nonnulls(instrument_id, option_contract_id) = 1),
  constraint alerts_channels_nonempty check (cardinality(channels) > 0),
  constraint alerts_id_user_unique unique (id, user_id)
);

create table if not exists public.alert_deliveries (
  id uuid primary key default gen_random_uuid(), alert_id uuid not null, user_id uuid not null references auth.users(id) on delete cascade,
  event_key text not null, channel text not null check (channel in ('in_app','email','push','sms','webhook')),
  status text not null default 'queued' check (status in ('queued','sending','delivered','failed','canceled')),
  provider_timestamp timestamptz, ingested_at timestamptz not null default now(), delivered_at timestamptz,
  attempt_count integer not null default 0 check (attempt_count >= 0), error_code text, error_message text,
  payload jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now(), updated_at timestamptz not null default now(),
  constraint alert_deliveries_owner_fk foreign key (alert_id, user_id) references public.alerts(id, user_id) on delete cascade,
  constraint alert_deliveries_event_channel_unique unique (alert_id, event_key, channel)
);

create table if not exists public.user_preferences (
  user_id uuid primary key references auth.users(id) on delete cascade,
  timezone text not null default 'Asia/Kolkata', locale text not null default 'en-IN', currency char(3) not null default 'INR',
  notification_preferences jsonb not null default '{}'::jsonb, display_preferences jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now(), updated_at timestamptz not null default now()
);

create table if not exists public.api_usage (
  id uuid primary key default gen_random_uuid(), user_id uuid references auth.users(id) on delete set null,
  request_key text not null unique, api_version text not null, route text not null, http_method text not null,
  status_code smallint, request_count integer not null default 1 check (request_count > 0), duration_ms integer,
  provider_id uuid references public.market_data_providers(id) on delete set null,
  provider_timestamp timestamptz, ingested_at timestamptz not null default now(), metadata jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now(), updated_at timestamptz not null default now(),
  constraint api_usage_status_check check (status_code is null or status_code between 100 and 599),
  constraint api_usage_duration_check check (duration_ms is null or duration_ms >= 0)
);

create table if not exists public.system_events (
  id uuid primary key default gen_random_uuid(), event_key text not null unique, event_type text not null,
  severity text not null default 'info' check (severity in ('debug','info','warning','error','critical')),
  provider_id uuid references public.market_data_providers(id) on delete set null,
  provider_timestamp timestamptz, ingested_at timestamptz not null default now(), payload jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now(), updated_at timestamptz not null default now()
);

create table if not exists public.audit_logs (
  id uuid primary key default gen_random_uuid(), actor_user_id uuid references auth.users(id) on delete set null,
  event_key text not null unique, action text not null, resource_type text not null, resource_id text,
  provider_timestamp timestamptz, ingested_at timestamptz not null default now(), ip_address inet, user_agent text,
  before_state jsonb, after_state jsonb, metadata jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now(), updated_at timestamptz not null default now()
);

-- Unique indexes make user additions and retries idempotent, including nullable subjects.
create unique index if not exists watchlist_items_instrument_unique
  on public.watchlist_items(watchlist_id, instrument_id) where instrument_id is not null;
create unique index if not exists watchlist_items_option_unique
  on public.watchlist_items(watchlist_id, option_contract_id) where option_contract_id is not null;
create unique index if not exists expired_obs_contract_idempotency_unique
  on public.expired_option_observations(provider_id, option_contract_id, provider_timestamp, observation_kind)
  where option_contract_id is not null;
create unique index if not exists expired_obs_unresolved_idempotency_unique
  on public.expired_option_observations(provider_id, underlying_id, option_expiry_id, strike, option_type, provider_timestamp, observation_kind)
  where option_contract_id is null;
create unique index if not exists target_events_session_time_unique
  on public.target_events(session_id, event_type, provider_timestamp) where provider_timestamp is not null;
create unique index if not exists scanner_results_user_idempotency_unique
  on public.scanner_results(user_id, scanner_code, result_key) where user_id is not null;
create unique index if not exists scanner_results_shared_idempotency_unique
  on public.scanner_results(scanner_code, result_key) where user_id is null;

-- Lookup indexes cover common joins and time-series access patterns.
create index if not exists subscriptions_user_status_idx on public.subscriptions(user_id, status);
create index if not exists instruments_provider_segment_symbol_idx on public.instruments(provider_id, exchange_segment, symbol);
create index if not exists underlying_assets_root_symbol_idx on public.underlying_assets(root_symbol);
create index if not exists option_expiries_underlying_date_idx on public.option_expiries(underlying_id, expiry_date);
create index if not exists live_snapshots_instrument_provider_time_idx on public.live_market_snapshots(instrument_id, provider_timestamp desc);
create index if not exists live_snapshots_provider_ingested_idx on public.live_market_snapshots(provider_id, ingested_at desc);
create index if not exists live_snapshots_time_brin_idx on public.live_market_snapshots using brin(provider_timestamp);
create index if not exists option_chain_underlying_expiry_strike_type_time_idx on public.option_chain_snapshots(underlying_id, option_expiry_id, strike, option_type, provider_timestamp desc);
create index if not exists option_chain_contract_time_idx on public.option_chain_snapshots(option_contract_id, provider_timestamp desc);
create index if not exists option_chain_provider_ingested_idx on public.option_chain_snapshots(provider_id, ingested_at desc);
create index if not exists historical_candles_instrument_time_idx on public.historical_candles(instrument_id, timeframe, candle_timestamp desc);
create index if not exists historical_candles_time_brin_idx on public.historical_candles using brin(candle_timestamp);
create index if not exists expired_options_underlying_expiry_strike_type_time_idx on public.expired_option_observations(underlying_id, option_expiry_id, strike, option_type, provider_timestamp desc);
create index if not exists ingestion_jobs_claim_idx on public.historical_ingestion_jobs(status, available_at, created_at) where status in ('queued','failed','partial');
create index if not exists ingestion_jobs_owner_created_idx on public.historical_ingestion_jobs(requested_by, created_at desc);
create index if not exists watchlists_user_updated_idx on public.watchlists(user_id, updated_at desc);
create index if not exists watchlist_items_watchlist_order_idx on public.watchlist_items(watchlist_id, display_order);
create index if not exists target_sessions_owner_status_idx on public.target_tracking_sessions(user_id, status, created_at desc);
create index if not exists target_sessions_instrument_status_idx on public.target_tracking_sessions(instrument_id, status) where instrument_id is not null;
create index if not exists target_sessions_contract_status_idx on public.target_tracking_sessions(option_contract_id, status) where option_contract_id is not null;
create index if not exists target_events_owner_created_idx on public.target_events(user_id, created_at desc);
create index if not exists target_events_session_created_idx on public.target_events(session_id, created_at desc);
create index if not exists scanner_results_instrument_time_idx on public.scanner_results(instrument_id, provider_timestamp desc) where instrument_id is not null;
create index if not exists scanner_results_subject_time_idx on public.scanner_results(underlying_id, option_contract_id, provider_timestamp desc);
create index if not exists scanner_results_user_time_idx on public.scanner_results(user_id, provider_timestamp desc);
create index if not exists opportunity_scores_underlying_time_idx on public.opportunity_scores(underlying_id, provider_timestamp desc);
create index if not exists opportunity_scores_contract_time_idx on public.opportunity_scores(option_contract_id, provider_timestamp desc);
create index if not exists alerts_owner_status_idx on public.alerts(user_id, status, created_at desc);
create index if not exists alert_deliveries_owner_status_idx on public.alert_deliveries(user_id, status, created_at desc);
create index if not exists alert_deliveries_retry_idx on public.alert_deliveries(status, created_at) where status in ('queued','failed');
create index if not exists api_usage_owner_created_idx on public.api_usage(user_id, created_at desc);
create index if not exists api_usage_created_brin_idx on public.api_usage using brin(created_at);
create index if not exists system_events_type_created_idx on public.system_events(event_type, created_at desc);
create index if not exists audit_logs_actor_created_idx on public.audit_logs(actor_user_id, created_at desc);
create index if not exists audit_logs_resource_created_idx on public.audit_logs(resource_type, resource_id, created_at desc);
create index if not exists subscriptions_plan_idx on public.subscriptions(plan_id);
create index if not exists watchlist_items_instrument_idx on public.watchlist_items(instrument_id) where instrument_id is not null;
create index if not exists watchlist_items_contract_idx on public.watchlist_items(option_contract_id) where option_contract_id is not null;

-- Maintain updated_at uniformly while retaining immutable provider/ingestion timestamps.
do $$
declare table_name text;
begin
  foreach table_name in array array[
    'profiles','subscription_plans','subscriptions','market_data_providers','instruments','underlying_assets',
    'option_expiries','option_contracts','live_market_snapshots','option_chain_snapshots','historical_candles',
    'expired_option_observations','historical_ingestion_jobs','watchlists','watchlist_items',
    'target_tracking_sessions','target_events','scanner_results','opportunity_scores','alerts',
    'alert_deliveries','user_preferences','api_usage','system_events','audit_logs'
  ] loop
    execute format('drop trigger if exists set_updated_at on public.%I', table_name);
    execute format('create trigger set_updated_at before update on public.%I for each row execute function public.set_updated_at()', table_name);
  end loop;
end;
$$;

-- Per-user rows are isolated by auth.uid(); owner pairs in foreign keys prevent an item,
-- event or delivery from being attached to another user's parent row.
drop policy if exists profiles_select_self on public.profiles;
create policy profiles_select_self on public.profiles for select to authenticated using (id = (select auth.uid()));
drop policy if exists profiles_insert_self on public.profiles;
create policy profiles_insert_self on public.profiles for insert to authenticated with check (id = (select auth.uid()));
drop policy if exists profiles_update_self on public.profiles;
create policy profiles_update_self on public.profiles for update to authenticated using (id = (select auth.uid())) with check (id = (select auth.uid()));

drop policy if exists subscription_plans_read_active on public.subscription_plans;
create policy subscription_plans_read_active on public.subscription_plans for select to authenticated using (is_active);
drop policy if exists subscriptions_read_self on public.subscriptions;
create policy subscriptions_read_self on public.subscriptions for select to authenticated using (user_id = (select auth.uid()));

drop policy if exists instruments_read_authenticated on public.instruments;
create policy instruments_read_authenticated on public.instruments for select to authenticated using (is_active);
drop policy if exists underlyings_read_authenticated on public.underlying_assets;
create policy underlyings_read_authenticated on public.underlying_assets for select to authenticated using (true);
drop policy if exists expiries_read_authenticated on public.option_expiries;
create policy expiries_read_authenticated on public.option_expiries for select to authenticated using (true);
drop policy if exists contracts_read_authenticated on public.option_contracts;
create policy contracts_read_authenticated on public.option_contracts for select to authenticated using (true);
drop policy if exists live_snapshots_read_authenticated on public.live_market_snapshots;
create policy live_snapshots_read_authenticated on public.live_market_snapshots for select to authenticated using (true);
drop policy if exists option_chain_read_authenticated on public.option_chain_snapshots;
create policy option_chain_read_authenticated on public.option_chain_snapshots for select to authenticated using (true);
drop policy if exists candles_read_authenticated on public.historical_candles;
create policy candles_read_authenticated on public.historical_candles for select to authenticated using (true);
drop policy if exists expired_options_read_authenticated on public.expired_option_observations;
create policy expired_options_read_authenticated on public.expired_option_observations for select to authenticated using (true);

drop policy if exists ingestion_jobs_read_owner on public.historical_ingestion_jobs;
create policy ingestion_jobs_read_owner on public.historical_ingestion_jobs for select to authenticated using (requested_by = (select auth.uid()));
drop policy if exists ingestion_jobs_insert_owner on public.historical_ingestion_jobs;
create policy ingestion_jobs_insert_owner on public.historical_ingestion_jobs for insert to authenticated with check (requested_by = (select auth.uid()));

drop policy if exists watchlists_owner_all on public.watchlists;
create policy watchlists_owner_all on public.watchlists for all to authenticated using (user_id = (select auth.uid())) with check (user_id = (select auth.uid()));
drop policy if exists watchlist_items_owner_all on public.watchlist_items;
create policy watchlist_items_owner_all on public.watchlist_items for all to authenticated using (user_id = (select auth.uid())) with check (user_id = (select auth.uid()));
drop policy if exists target_sessions_owner_all on public.target_tracking_sessions;
create policy target_sessions_owner_all on public.target_tracking_sessions for all to authenticated using (user_id = (select auth.uid())) with check (user_id = (select auth.uid()));
drop policy if exists target_events_owner_read on public.target_events;
create policy target_events_owner_read on public.target_events for select to authenticated using (user_id = (select auth.uid()));

drop policy if exists scanner_results_owner_or_shared_read on public.scanner_results;
create policy scanner_results_owner_or_shared_read on public.scanner_results for select to authenticated using (user_id is null or user_id = (select auth.uid()));
drop policy if exists opportunity_scores_owner_or_shared_read on public.opportunity_scores;
create policy opportunity_scores_owner_or_shared_read on public.opportunity_scores for select to authenticated using (user_id is null or user_id = (select auth.uid()));

drop policy if exists alerts_owner_all on public.alerts;
create policy alerts_owner_all on public.alerts for all to authenticated using (user_id = (select auth.uid())) with check (user_id = (select auth.uid()));
drop policy if exists alert_deliveries_owner_read on public.alert_deliveries;
create policy alert_deliveries_owner_read on public.alert_deliveries for select to authenticated using (user_id = (select auth.uid()));
drop policy if exists preferences_owner_all on public.user_preferences;
create policy preferences_owner_all on public.user_preferences for all to authenticated using (user_id = (select auth.uid())) with check (user_id = (select auth.uid()));
drop policy if exists api_usage_owner_read on public.api_usage;
create policy api_usage_owner_read on public.api_usage for select to authenticated using (user_id = (select auth.uid()));

-- RLS is default-deny for market provider configuration, system events and audit logs.
grant usage on schema public to authenticated, service_role;
grant select on public.subscription_plans, public.subscriptions, public.instruments, public.underlying_assets,
  public.option_expiries, public.option_contracts, public.live_market_snapshots, public.option_chain_snapshots,
  public.historical_candles, public.expired_option_observations, public.historical_ingestion_jobs,
  public.target_events, public.scanner_results, public.opportunity_scores, public.alert_deliveries,
  public.api_usage to authenticated;
grant insert on public.historical_ingestion_jobs to authenticated;

grant select on public.profiles to authenticated;
grant insert (id, display_name, avatar_url) on public.profiles to authenticated;
grant update (display_name, avatar_url) on public.profiles to authenticated;

grant select on public.watchlists to authenticated;
grant insert (user_id, name, description, is_default) on public.watchlists to authenticated;
grant update (name, description, is_default) on public.watchlists to authenticated;
grant delete on public.watchlists to authenticated;
grant select on public.watchlist_items to authenticated;
grant insert (user_id, watchlist_id, instrument_id, option_contract_id, display_order, note) on public.watchlist_items to authenticated;
grant update (display_order, note) on public.watchlist_items to authenticated;
grant delete on public.watchlist_items to authenticated;

grant select on public.target_tracking_sessions to authenticated;
grant insert (user_id, instrument_id, option_contract_id, direction, target_price, starts_at, expires_at, metadata) on public.target_tracking_sessions to authenticated;
grant update (direction, target_price, expires_at, metadata) on public.target_tracking_sessions to authenticated;
grant delete on public.target_tracking_sessions to authenticated;
grant select on public.alerts to authenticated;
grant insert (user_id, name, alert_type, instrument_id, option_contract_id, condition, channels) on public.alerts to authenticated;
grant update (name, condition, channels, status) on public.alerts to authenticated;
grant delete on public.alerts to authenticated;

grant select on public.user_preferences to authenticated;
grant insert (user_id, timezone, locale, currency, notification_preferences, display_preferences) on public.user_preferences to authenticated;
grant update (timezone, locale, currency, notification_preferences, display_preferences) on public.user_preferences to authenticated;

grant all privileges on public.profiles, public.subscription_plans, public.subscriptions, public.market_data_providers,
  public.instruments, public.underlying_assets, public.option_expiries, public.option_contracts,
  public.live_market_snapshots, public.option_chain_snapshots, public.historical_candles,
  public.expired_option_observations, public.historical_ingestion_jobs, public.watchlists,
  public.watchlist_items, public.target_tracking_sessions, public.target_events, public.scanner_results,
  public.opportunity_scores, public.alerts, public.alert_deliveries, public.user_preferences,
  public.api_usage, public.system_events to service_role;
grant select, insert on public.audit_logs to service_role;
revoke update, delete, truncate on public.audit_logs from service_role;

-- Never expose provider configuration, service telemetry, or audit state to browser roles.
revoke all on public.market_data_providers, public.system_events, public.audit_logs from anon, authenticated;

-- Supabase Auth creates an application profile on signup. Metadata is informational only.
create or replace function public.handle_new_auth_user()
returns trigger language plpgsql security definer set search_path = public, pg_temp as $$
begin
  insert into public.profiles (id, email, display_name)
  values (new.id, new.email, nullif(new.raw_user_meta_data ->> 'full_name', ''))
  on conflict (id) do nothing;
  return new;
end;
$$;
revoke all on function public.handle_new_auth_user() from public, anon, authenticated;
drop trigger if exists on_auth_user_created_market_pulse on auth.users;
create trigger on_auth_user_created_market_pulse after insert on auth.users
for each row execute function public.handle_new_auth_user();

-- Enable RLS on every application table.
do $$
declare table_name text;
begin
  foreach table_name in array array[
    'profiles','subscription_plans','subscriptions','market_data_providers','instruments','underlying_assets',
    'option_expiries','option_contracts','live_market_snapshots','option_chain_snapshots','historical_candles',
    'expired_option_observations','historical_ingestion_jobs','watchlists','watchlist_items',
    'target_tracking_sessions','target_events','scanner_results','opportunity_scores','alerts',
    'alert_deliveries','user_preferences','api_usage','system_events','audit_logs'
  ] loop execute format('alter table public.%I enable row level security', table_name); end loop;
end;
$$;

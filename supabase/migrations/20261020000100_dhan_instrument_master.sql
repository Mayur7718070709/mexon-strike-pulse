-- Dhan instrument-master lifecycle. Rows are never deleted so a retired
-- security-id mapping remains available to historical observations.
alter table public.instruments
  add column if not exists instrument_type text,
  add column if not exists underlying_symbol text,
  add column if not exists expiry_date date,
  add column if not exists strike_price numeric(24,8),
  add column if not exists option_type text,
  add column if not exists source text,
  add column if not exists synchronized_at timestamptz,
  add column if not exists inactive_at timestamptz;

alter table public.option_contracts
  alter column lot_size drop not null,
  add column if not exists is_active boolean not null default true,
  add column if not exists is_expired boolean not null default false,
  add column if not exists source text,
  add column if not exists synchronized_at timestamptz,
  add column if not exists inactive_at timestamptz;

alter table public.option_expiries
  add column if not exists source text,
  add column if not exists synchronized_at timestamptz;

create index if not exists instruments_underlying_expiry_strike_type_idx
  on public.instruments(underlying_symbol, expiry_date, strike_price, option_type, synchronized_at desc);
create index if not exists instruments_active_segment_type_symbol_idx
  on public.instruments(is_active, exchange_segment, instrument_type, symbol);
create index if not exists option_contracts_underlying_expiry_strike_type_idx
  on public.option_contracts(underlying_id, option_expiry_id, strike, option_type);
create index if not exists option_contracts_expired_idx
  on public.option_contracts(is_expired, is_active, synchronized_at desc);

comment on column public.instruments.synchronized_at is 'UTC ingestion time when this provider mapping was last confirmed by the official Dhan master; not an exchange event timestamp.';
comment on column public.instruments.source is 'Provider that supplied the instrument mapping, currently DHANHQ.';
comment on column public.option_contracts.synchronized_at is 'UTC ingestion time when this provider mapping was last confirmed by the official Dhan master.';
comment on column public.option_expiries.synchronized_at is 'UTC ingestion time when Dhan confirmed this expiry through the instrument master or expiry-list endpoint.';

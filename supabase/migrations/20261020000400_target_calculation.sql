-- Premium-multiple tracking levels are stored separately for each user's CALL/PUT contract.
alter table public.target_tracking_sessions
  add column if not exists reference_premium numeric(24,8),
  add column if not exists reference_timestamp timestamptz,
  add column if not exists underlying_id uuid references public.underlying_assets(id) on delete restrict,
  add column if not exists option_expiry_id uuid,
  add column if not exists strike numeric(24,8),
  add column if not exists option_type text,
  add column if not exists source_observation jsonb not null default '{}'::jsonb,
  add column if not exists verification_status text not null default 'unverified',
  add constraint target_session_reference_positive check (reference_premium is null or reference_premium > 0),
  add constraint target_session_strike_nonnegative check (strike is null or strike >= 0),
  add constraint target_session_option_type_check check (option_type is null or option_type in ('call','put')),
  add constraint target_session_verification_check check (verification_status in ('verified','unverified'));

alter table public.target_tracking_sessions
  add constraint target_session_expiry_underlying_fk foreign key (option_expiry_id,underlying_id)
    references public.option_expiries(id,underlying_id) on delete restrict,
  add constraint target_session_contract_identity_fk foreign key
    (option_contract_id,underlying_id,option_expiry_id,strike,option_type)
    references public.option_contracts(id,underlying_id,option_expiry_id,strike,option_type) on delete restrict;

create or replace function public.prevent_target_reference_mutation()
returns trigger language plpgsql as $$
begin
  if old.reference_premium is not null and (
    new.reference_premium is distinct from old.reference_premium or
    new.reference_timestamp is distinct from old.reference_timestamp or
    new.instrument_id is distinct from old.instrument_id or
    new.option_contract_id is distinct from old.option_contract_id or
    new.underlying_id is distinct from old.underlying_id or
    new.option_expiry_id is distinct from old.option_expiry_id or
    new.strike is distinct from old.strike or
    new.option_type is distinct from old.option_type or
    new.source_observation is distinct from old.source_observation
  ) then
    raise exception 'target session reference observation is immutable';
  end if;
  return new;
end;
$$;
drop trigger if exists target_session_reference_immutable on public.target_tracking_sessions;
create trigger target_session_reference_immutable before update on public.target_tracking_sessions
  for each row execute function public.prevent_target_reference_mutation();

create table if not exists public.target_tracking_levels (
  id uuid primary key default gen_random_uuid(),
  session_id uuid not null,
  user_id uuid not null references auth.users(id) on delete cascade,
  multiplier smallint not null check (multiplier in (2,3,5,7,10)),
  target_value numeric(24,8) not null check (target_value > 0),
  status text not null default 'NOT_REACHED' check (status in ('NOT_REACHED','REACHED','UNVERIFIED')),
  first_reached_at timestamptz,
  source_observation jsonb,
  last_observed_at timestamptz,
  last_observation jsonb,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  constraint target_level_owner_session_fk foreign key (session_id,user_id)
    references public.target_tracking_sessions(id,user_id) on delete cascade,
  constraint target_level_session_multiplier_unique unique (session_id,multiplier),
  constraint target_level_reach_state_check check (
    (status='REACHED' and first_reached_at is not null and source_observation is not null) or
    (status in ('NOT_REACHED','UNVERIFIED'))
  )
);

create index if not exists target_levels_owner_session_idx
  on public.target_tracking_levels(user_id,session_id,multiplier);
create index if not exists target_levels_pending_idx
  on public.target_tracking_levels(session_id,status) where first_reached_at is null;

drop trigger if exists set_updated_at on public.target_tracking_levels;
create trigger set_updated_at before update on public.target_tracking_levels
  for each row execute function public.set_updated_at();

create or replace function public.protect_reached_target_level()
returns trigger language plpgsql as $$
begin
  if new.session_id is distinct from old.session_id or new.user_id is distinct from old.user_id or
     new.multiplier is distinct from old.multiplier or new.target_value is distinct from old.target_value then
    raise exception 'target level definition is immutable';
  end if;
  if old.first_reached_at is not null and (
    new.first_reached_at is distinct from old.first_reached_at or new.status <> 'REACHED' or
    new.source_observation is distinct from old.source_observation
  ) then
    raise exception 'first verified target reach is immutable';
  end if;
  return new;
end;
$$;
drop trigger if exists target_level_protect_reached on public.target_tracking_levels;
create trigger target_level_protect_reached before update on public.target_tracking_levels
  for each row execute function public.protect_reached_target_level();

alter table public.target_tracking_levels enable row level security;
drop policy if exists target_levels_owner_read on public.target_tracking_levels;
create policy target_levels_owner_read on public.target_tracking_levels
  for select to authenticated using (user_id=(select auth.uid()));
grant select on public.target_tracking_levels to authenticated;
grant all on public.target_tracking_levels to service_role;
grant all on public.target_tracking_sessions to service_role;

comment on column public.target_tracking_sessions.reference_premium is 'Immutable provider-observed option premium captured when the tracking session was created.';
comment on column public.target_tracking_sessions.reference_timestamp is 'Original Dhan exchange timestamp for the reference premium.';
comment on column public.target_tracking_sessions.source_observation is 'Dhan source observation at tracking start; excludes credentials.';
comment on table public.target_tracking_levels is 'Per-session premium multipliers. These are tracking levels, not predictions or guarantees.';

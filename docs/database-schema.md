# Supabase PostgreSQL schema

The initial schema lives in `supabase/migrations/20261019000100_market_pulse_core.sql` and uses the Supabase CLI migration format. Apply it through the normal project migration pipeline (`supabase link --project-ref <project-ref>` followed by `supabase db push`) after reviewing it against the target project's existing schema. This migration contains DDL only; it creates no market rows, test users, or mock providers.

## Data boundaries

- `live_market_snapshots` and `option_chain_snapshots` retain provider updates. `historical_candles` and `expired_option_observations` retain historical data separately.
- Provider event time is stored in `provider_timestamp`; database receipt time is stored in `ingested_at`. Both use `timestamptz`, so PostgreSQL stores absolute instants and presents them in the connection timezone.
- Historical candles are idempotent on `(provider_id, instrument_id, timeframe, candle_timestamp)`. Ingestion jobs also have a unique caller-supplied `idempotency_key`.
- `target_events.event_key` is globally unique. A second unique key prevents duplicate event types for the same target session and provider timestamp. Alert delivery uniqueness is `(alert_id, event_key, channel)`.
- Option contracts bind to an expiry belonging to the same underlying. Option chain and expired observation rows use composite foreign keys to keep their expiry, underlying, strike and option type consistent with that contract.

## Row security

RLS is enabled on every public application table. Profiles, subscriptions, watchlists, target sessions, alerts, preferences and user activity are scoped to `auth.uid()`. Composite owner foreign keys additionally prevent watchlist items, target events and alert deliveries from referencing another user's parent row. Shared market catalogs and observations are readable to authenticated users; provider configuration, system events and audit logs have no browser-role policy. Provider and billing data writes are intended for trusted backend operations.

The `service_role` bypasses RLS. Keep it exclusively in trusted server-side infrastructure. It must never appear in frontend environment variables or browser bundles. The migration grants the service role table access but does not provision or store a service-role key.

## Tables

The migration defines `profiles`, `subscription_plans`, `subscriptions`, `market_data_providers`, `instruments`, `underlying_assets`, `option_expiries`, `option_contracts`, `live_market_snapshots`, `option_chain_snapshots`, `historical_candles`, `expired_option_observations`, `historical_ingestion_jobs`, `watchlists`, `watchlist_items`, `target_tracking_sessions`, `target_events`, `scanner_results`, `opportunity_scores`, `alerts`, `alert_deliveries`, `user_preferences`, `api_usage`, `system_events` and `audit_logs`.

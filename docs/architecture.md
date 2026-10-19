# Phase 1 architecture

The browser talks only to the versioned FastAPI service. FastAPI owns all DhanHQ connections, Redis cache/event distribution and PostgreSQL access. A Dhan live feed connection subscribes only to explicitly configured real instruments. Redis stores the most recently received provider packet with its Dhan exchange timestamp and server receipt timestamp. The API returns a data status and timestamps with every quote; it never synthesizes missing values.

```text
Next.js PWA → FastAPI /api/v1 → Redis (latest ticks/events)
                                ├→ DhanHQ v2 WebSocket (backend only)
                                └→ Supabase PostgreSQL (SQLAlchemy)
Redis → arq worker (future ingestion, targets and alerts)
```

The API and worker report dependency health independently. Redis/PostgreSQL connection failures are surfaced in health output. The browser signs in through Supabase Auth and sends access tokens to protected market routes. FastAPI validates asymmetric JWTs against the [project JWKS](https://supabase.com/docs/guides/auth/signing-keys) and checks issuer, audience and expiry. For legacy HS256 sessions, it asks Supabase Auth to validate the token as recommended in the [JWT guide](https://supabase.com/docs/guides/auth/jwts). Health and generated OpenAPI remain public.

## Data freshness

Every quote has `source`, `received_at`, and `exchange_timestamp`. The API returns `DATA UNAVAILABLE` when there is no Dhan packet and `DATA STALE` after `MARKET_DATA_STALE_AFTER_SECONDS`. Only a recently received Dhan packet is marked `LIVE`. A stale cached packet remains labeled stale with its original timestamps.

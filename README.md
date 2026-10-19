# Market Pulse

Market and options intelligence by Mexon Intelligence Pvt Ltd. Phase 1 establishes a responsive web client and versioned Python API. Market data is sourced only from DhanHQ v2. Missing configuration or unavailable dependencies produce explicit unavailable status; no prices are seeded or simulated.

## Repository layout

- `frontend/` Next.js mobile-first PWA foundation
- `backend/` FastAPI, SQLAlchemy, Supabase PostgreSQL, Redis and DhanHQ feed adapter
- `workers/` Redis-backed background worker entry point
- `shared/` API contract notes
- `docs/` architecture and official DhanHQ contract verification
- `tests/` backend unit and API tests
- `infrastructure/` container orchestration

## Run locally

1. Copy `.env.example` to `.env` and supply actual Supabase/Dhan credentials. Set `DHAN_INSTRUMENTS` to the actual Dhan exchange segment/security IDs you want to monitor.
2. Start services with `docker compose up --build`.
3. Open `http://localhost:3000`; API docs are at `http://localhost:8000/docs` and health is at `http://localhost:8000/api/v1/health`.

Market prices remain `DATA UNAVAILABLE` until Supabase Auth is configured, a user signs in, Dhan credentials and at least one instrument are configured, and a live packet arrives. On disconnect, the last Dhan timestamp is preserved and the data changes to `DATA STALE` after the configured threshold. Cached data is never labeled live.

## Tests

Backend tests: `python -m pytest` from the repository root. Tests validate behavior and packet decoding without calling Dhan or fabricating successful market responses. Frontend: `cd frontend && npm run build`.

## Database

Version-controlled Supabase migrations are under `supabase/migrations/`. See [database schema notes](docs/database-schema.md) before applying them to a Supabase project. Database credentials and any service-role credential belong in backend-only deployment secrets; never in `NEXT_PUBLIC_*` variables.

## Scope

No order placement or trading endpoints are implemented. Supabase Auth protects market data requests; Dhan credentials are backend-only.

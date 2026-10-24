# Premium-multiple targets

`TargetCalculationService` calculates each option side independently using the option premium as the only input:

```text
target = reference_premium × multiplier
```

Supported multipliers are 2X, 3X, 5X, 7X and 10X. The strike is stored as contract identity only and is never an input to the formula. Monetary values use `Decimal` and the database's eight fractional digits. Invalid, zero, negative, non-finite, or over-precision reference premiums are rejected.

Example formula checks (not market observations): CALL ₹120 × 10 = ₹1,200; PUT ₹95 × 10 = ₹950.

## Live reference and lifecycle

`POST /api/v1/targets/start` accepts an idempotency UUID and underlying, expiry, strike and CALL/PUT identity. The backend resolves the exact active contract from synchronized Dhan records. The first request displays a fresh, positive Dhan LTP with exchange and ingestion timestamps and does not persist a session. The user confirms the exact premium/timestamp pair in a second request; if it changed or aged out, the latest quote must be confirmed again. The backend never trusts a client-provided premium as market data.

The session preserves the original reference premium, provider timestamp and source observation. Its five target levels are stored separately. Fresh Dhan ticks update latest premium, current multiple and unreached levels; the first source timestamp reaching each target is retained with that observation. A later stale or missing quote marks unreached levels `UNVERIFIED` and never erases a previously reached timestamp. Sessions remain active after an individual level is reached so higher levels continue tracking. A session ends through explicit user cancellation or after contract expiry; starting a new session creates a separate reference and history. Replaying the same request UUID returns its original session rather than changing the baseline.

Targets are tracking levels, not predictions or guarantees. Statuses report whether fresh provider evidence has or has not verified a level. They do not imply execution, fill, or future price movement.

## API

- `POST /api/v1/targets/start` — display then confirm a fresh Dhan quote before creating the session. See [user watchlists and target tracking](user-watchlists-and-targets.md) for request bodies.
- `GET /api/v1/targets` — list the caller's sessions, latest observation and target verification state.
- `POST /api/v1/targets/{target_id}/stop` — explicitly stop a session.
- `GET /api/v1/target-sessions` and `GET /api/v1/target-sessions/{session_id}` remain read aliases for older clients; the detail route refreshes level states from the latest timestamped Dhan quote.
- `POST /api/v1/target-sessions/{session_id}/cancel` — explicit user cancellation alias. There is no reset operation.

Target rows carry the same `user_id` as their parent session. RLS permits users to read only their own levels; only the trusted backend writes calculation and observation state. Dhan feed IDs are resolved from synchronized instrument records, and credentials remain server-side.

The schema is in `supabase/migrations/20261020000400_target_calculation.sql` and `supabase/migrations/20261021000100_user_watchlists_target_start.sql`. Formula-only unit tests are in `tests/test_target_calculations.py`; they do not represent market data.

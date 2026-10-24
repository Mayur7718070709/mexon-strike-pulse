# User Watchlists and Target Tracking

All routes are under `/api/v1` and require a valid Supabase Auth bearer token. The API derives user ownership from the verified token; caller-supplied `user_id` values are rejected by request validation.

## Watchlists

- `GET /watchlists` returns only the caller's watchlists and items.
- `POST /watchlists` creates a list with `{ "name": "My Watchlist", "description": "optional" }`.
- `POST /watchlists/{watchlist_id}/items` adds exactly one active Dhan-mapped subject. Supply either `instrument_id`, `option_contract_id`, or the complete option identity `{ "underlying", "expiry", "strike", "option_type" }`.
- `DELETE /watchlists/{watchlist_id}/items/{item_id}` removes an item only when both list and item belong to the caller.

Option identity is resolved against the synchronized Dhan instrument master. Expired, inactive and unsupported mappings are rejected. An item can exist while its quote is `DATA UNAVAILABLE`; it is never assigned a made-up price. Quote projections return the original Dhan exchange timestamp and backend receive timestamp. A quote is labeled `LIVE` only while both timestamps are within the configured freshness threshold; older provider observations are labeled `DATA STALE`.

## Target sessions

`POST /targets/start` takes a client-generated idempotency UUID and a Dhan-mapped contract identity:

```json
{
  "request_id": "<uuid>",
  "underlying": "NIFTY",
  "expiry": "YYYY-MM-DD",
  "strike": "<positive decimal>",
  "option_type": "CALL"
}
```

The first call returns the current positive Dhan premium, exchange timestamp, receive timestamp and calculated 2X/3X/5X/7X/10X levels with `confirmation_required: true`; it does not create a session. The user confirms that exact premium/timestamp in a second request by adding `confirmed_reference_premium` and `confirmed_reference_timestamp`. If the quote changes or becomes stale, no baseline is written and the client must display and confirm a new observation. When Dhan has not sent a fresh observation yet, the server requests a subscription to the exact synchronized contract and returns unavailable; the client may retry after a quote arrives.

The session stores its confirmed baseline immutably. Replaying `request_id` returns the existing session instead of resetting its baseline. A PostgreSQL unique index enforces this idempotency. `GET /targets` returns only the caller's sessions, including latest premium, provider timestamp, ingestion timestamp, current multiple, data quality, session status, each target status and the first provider timestamp at which the level was reached. `POST /targets/{target_id}/stop` is an explicit user action. There is no reset operation.

The background feed evaluates targets only from matching Dhan `NSE_FNO` quotes. A unique `target_events.event_key` and an `ON CONFLICT DO NOTHING` insert prevent duplicate target-reached events. On feed staleness, unreached levels become `UNVERIFIED`; the latest actual observation and its timestamps remain visible as stale. Sessions expire after their contract expiry date, or at 15:30 Asia/Kolkata on expiry day. First verified reach timestamps are preserved.

## Data ownership and security

Watchlists, their items, target sessions, target levels and target events use the existing `auth.uid()` owner RLS policies and owner-scoped composite foreign keys. The API also applies explicit verified `user_id` filters on every user-data read, update and delete. Dhan credentials are never used or returned by these endpoints; the frontend sends only the Supabase user access token to FastAPI.

Migration: `20261021000100_user_watchlists_target_start.sql` adds the current observation fields and target-start idempotency key to `target_tracking_sessions`. Existing RLS and ownership policies are defined by the earlier core and target calculation migrations.

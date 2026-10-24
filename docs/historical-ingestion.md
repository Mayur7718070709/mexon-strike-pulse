# Historical Dhan data ingestion

Historical backfills use only the authenticated DhanHQ v2 endpoints through `DhanProvider`:

| Interval | Provider endpoint | Worker chunk size | Notes |
| --- | --- | --- | --- |
| `1`, `5`, `15`, `25`, `60` | `POST /charts/intraday` | At most 90 days | Endpoint rejects request windows over 90 days. |
| `1d` | `POST /charts/historical` | At most 365 days (application chunking) | Dhan documents history from instrument inception; availability is instrument-specific. |

Supported security IDs, segments, and instrument types are resolved from Dhan's detailed instrument master. A user-supplied security ID is never trusted as a mapping. Historical lookups are currently limited to the NSE index, equity, and futures/options types stored by the synchronized master. Intraday requires an active Dhan mapping, consistent with Dhan's documented availability. Daily history can use a retained inactive master mapping because Dhan documents daily data back to a scrip's inception. Any identifier not present in the master is unavailable.

The admin API accepts exactly one of `underlying` or `instrument`, plus `interval`, `from_date`, and `to_date`. `to_date` is exclusive, matching Dhan's contract. It creates a persistent ingestion job and chunks before enqueueing `run_historical_ingestion` in ARQ. The worker first requests up to the last seven days inside the requested range; it does not fetch earlier bulk chunks unless this real sample returns candles that pass validation. The sample chunk is included in the requested range and is not fetched twice. Sampling near the requested end lets a long intraday range still ingest provider-supported dates when its oldest requested dates predate Dhan's documented intraday window.

Minute chunks are never larger than the documented 90-day provider limit. Daily chunks are an application reliability choice, not a Dhan endpoint limit. Requests are processed serially through Dhan's shared `data` rate-limit bucket (5 requests/second and 100,000 requests/day); the REST client applies its bounded timeout/retry policy and each failed chunk gets one additional bounded worker attempt. No local data is synthesized when Dhan fails or returns no sample.

Each persisted candle stores the provider epoch timestamp as both `candle_timestamp` and `provider_timestamp`, and independently stamps `ingested_at` when normalized. The database unique key `(provider_id, instrument_id, timeframe, candle_timestamp)` makes retries idempotent. Ingestion chunk rows record requested boundaries, attempts, returned/accepted counts, invalid/duplicate counts, provider first/last timestamps, status, and failure details. Completed chunks are retained and skipped after restarts; failed or partial chunks can be retried by submitting the same request identity.

Coverage is calculated from stored provider-returned records and includes earliest/latest timestamps, observation count, and timestamp gaps. Intraday gaps are only detected within a session day; daily gaps ignore weekends. The market holiday calendar is not modeled, so a detected daily gap can include an exchange holiday. A five-year claim is never derived from a requested date range; it requires returned observations spanning that period.

## Admin endpoints

- `POST /api/v1/admin/historical/backfill` — admin-only, returns a job ID after queueing.
- `GET /api/v1/admin/historical/jobs/{job_id}` — progress, chunk failures, invalid/duplicate counts, and the current coverage report.
- `GET /api/v1/admin/historical/coverage?underlying=NIFTY&interval=1` — coverage for stored observations; use `instrument=` instead of `underlying=` for an exact mapped symbol.

The web admin panel is shown only when the Supabase session contains the server-managed `app_metadata.role=admin` claim. The API enforces the same role independently. Configure both the API and ARQ worker with the same server-side `DATABASE_URL`, Redis URL, `DHAN_CLIENT_ID`, and `DHAN_ACCESS_TOKEN`; credentials are never sent to the browser.

## Dhan contracts

- [Historical Data API v2](https://dhanhq.co/docs/v2/historical-data/) — endpoints, request fields, intervals, response arrays, inclusive/exclusive date behavior, and availability notes.
- [Instrument List](https://dhanhq.co/docs/v2/instruments/) and [Annexure](https://dhanhq.co/docs/v2/annexure/) — synchronized security mappings and exchange/instrument nomenclature.

# Dhan expired-options history

Market Pulse uses only the official DhanHQ v2 [Expired Options Data](https://dhanhq.co/docs/v2/expired-options-data/) endpoint: `POST /charts/rollingoption`.

## Request mapping

| Market Pulse field | Dhan request field | Validation |
| --- | --- | --- |
| `underlying` | `securityId` | Resolved from the synchronized Dhan detailed instrument master; user-supplied IDs are never accepted. |
| NSE option venue | `exchangeSegment` | `NSE_FNO` only. Index roots resolve from `IDX_I`; eligible stock roots resolve from `NSE_EQ`. MCX and other unsupported underlyings return `UNSUPPORTED`. |
| underlying kind | `instrument` | `OPTIDX` for a master-confirmed index root, `OPTSTK` for a master-confirmed equity root. |
| `interval` | `interval` | Dhan minute values `1`, `5`, `15`, `25`, `60`. |
| `expiry_flag` | `expiryFlag` | `WEEK` or `MONTH`. |
| `expiry_code` | `expiryCode` | Dhan's documented enum `0` current/near expiry, `1` next expiry, `2` far expiry. It is not converted to an expiry date. |
| `strike_selections[]` | `strike` | Dhan-relative `ATM`, `ATM+N`, `ATM-N`. Service permits ATM±10 only for master-confirmed index options with `expiry_code=0` (Dhan defines code 0 as current/near expiry); all other cases are limited to ATM±3. |
| `option_types[]` | `drvOptionType` | `CALL` and/or `PUT`, one request per side. |
| fixed fields | `requiredData` | Requests `open`, `high`, `low`, `close`, `iv`, `volume`, `strike`, `oi`, `spot`; each field is persisted as returned or null if absent. |
| requested range | `fromDate`, `toDate` | `toDate` is exclusive; each request is at most 30 calendar days. Requests older than the documented rolling five-year window or in the future are rejected. |

The worker stores the requested expiry selector and actual strike returned by Dhan. The endpoint response does not include a calendar expiry date, so `option_expiry_id` and the response `calendar_expiry_date` remain null unless a future documented provider field can prove that mapping. No date is inferred from expiry code or current instrument mappings.

## Sampling, durability, coverage

The first request is a real Dhan query for one requested strike selection and one option type over at most the last seven days of the requested range. A non-empty valid response is required before bulk chunks proceed. Each request is persisted as an independent chunk; failed and empty responses are recorded rather than converted into prices. ARQ recovery requeues interrupted jobs. Database unique indexes make repeated provider timestamps idempotent.

Rows retain `provider_timestamp` from Dhan epoch seconds and a separate `ingested_at`; OHLC, IV, volume, OI, strike, spot and source are retained. Missing values stay SQL `NULL`; observed zeros remain zero. A missing side or empty series is reported as no observations. Records live in `expired_option_observations`, never in live option-chain snapshots.

Coverage is computed from stored source timestamps. It includes actual earliest/latest times, counts, and within-session gaps; no returned rows means `NO OBSERVATIONS`. The premium-multiple analysis is grouped by source date, actual returned strike, relative strike selector and option side because the rolling endpoint does not return exact expiry dates. Within each group it uses that date's first stored candle open as entry premium and highest same-date candle high as peak. If the entry is zero/non-positive, no multiple is calculated. The calculation is descriptive and is not a recommendation or multi-day contract backtest.

## Admin API

- `POST /api/v1/admin/expired-options/backfill`
- `GET /api/v1/admin/expired-options/jobs/{job_id}`
- `GET /api/v1/admin/expired-options/jobs/{job_id}/analysis`
- `GET /api/v1/admin/expired-options/coverage?underlying=NIFTY&interval=1&expiry_flag=WEEK&expiry_code=0`

All routes require the server-validated Supabase admin role. Configure `DATABASE_URL`, Redis, `DHAN_CLIENT_ID`, and `DHAN_ACCESS_TOKEN` for both API and worker deployments. Dhan credentials are not returned to or used by frontend code.

## Official contract references

- [DhanHQ Expired Options Data v2](https://dhanhq.co/docs/v2/expired-options-data/) — endpoint, request fields, interval values, relative strikes, rolling range, response fields and timestamps.
- [DhanHQ Instrument List](https://dhanhq.co/docs/v2/instruments/) and [Annexure](https://dhanhq.co/docs/v2/annexure/) — provider-confirmed underlying IDs and supported instrument/segment mappings.
- [DhanHQ API rate limits](https://dhanhq.co/docs/v2/) — shared data API limit; requests use the existing provider rate limiter and bounded retry policy.

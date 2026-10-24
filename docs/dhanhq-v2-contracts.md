# DhanHQ v2 contracts checked

Checked against official DhanHQ v2 documentation on 2026-10-19:

- [Live Market Feed](https://dhanhq.co/docs/v2/live-market-feed/): backend WebSocket `wss://api-feed.dhan.co?version=2&token=...&clientId=...&authType=2`; subscription requests are JSON and response packets are binary little-endian. Subscription message uses `RequestCode`, `InstrumentCount`, and `InstrumentList` (`ExchangeSegment`, `SecurityId`). Maximums documented: 5 connections per user, 5000 instruments per connection, 100 instruments per subscription message. Ping arrives every 10 seconds; respond with WebSocket pong.
- Ticker subscription uses request code `15`. Ticker packet response code `2` contains an 8-byte common header, float32 LTP at bytes 9–12 and int32 epoch LTT at bytes 13–16. The optional previous-close packet has response code `6` and is not treated as an LTP.
- [Annexure](https://dhanhq.co/docs/v2/annexure/): the header exchange segment byte maps `0` to `IDX_I`, `1` to `NSE_EQ`, `2` to `NSE_FNO`, `3` to `NSE_CURRENCY`, `4` to `BSE_EQ`, `5` to `MCX_COMM`, `7` to `BSE_CURRENCY`, and `8` to `BSE_FNO`. Redis keys and UI display retain this segment with the security ID.
- [Market Quote](https://dhanhq.co/docs/v2/market-quote/): `POST https://api.dhan.co/v2/marketfeed/ltp`, `/ohlc`, and `/quote`; required headers `access-token` and `client-id`; request maps official exchange segment enums to arrays of numeric security IDs. Limit is 1000 instruments and 1 request/second.
- [Authentication](https://dhanhq.co/docs/v2/authentication/): access token is required for requests; individual web-generated tokens last 24 hours. Credentials remain server-side.

Phase 1 uses the documented ticker WebSocket flow only. It does not call order APIs. Binary decoding accepts only the documented ticker packet length/code and ignores other packet types; malformed packets are logged and not converted into prices. Dhan security IDs are operator-supplied from Dhan's official instrument list; no example IDs are seeded into runtime configuration.
# Authentication method configured for this deployment

Market Pulse uses the individual Dhan Web generated access-token method. Configure `DHAN_CLIENT_ID` and `DHAN_ACCESS_TOKEN` only as server-side secrets. Dhan documents these tokens as valid for 24 hours. The backend validates them by calling `GET https://api.dhan.co/v2/profile` with the `access-token` header; profile fields are checked against the configured client ID and the documented `tokenValidity`, `activeSegment`, `ddpi`, `mtf`, `dataPlan`, and `dataValidity` fields.

Token rotation is an operator action: generate a replacement token in Dhan Web, replace the backend secret `DHAN_ACCESS_TOKEN` (and client ID if applicable), then restart/redeploy the backend. Do not send credentials through the browser or a public API. This deployment does not store PINs, passwords, TOTP secrets, API keys, or API secrets and does not implement automatic renewal. Dhan's documented renew endpoint only applies to active tokens generated from Dhan Web and invalidates the current token while returning a replacement; automatic renewal is not enabled because there is no secure persistent token vault configured to atomically store and distribute that replacement.

`GET /api/v1/integrations/dhan/status` requires a valid Supabase user session. `LIVE CONNECTED` is returned only after a fresh successful Dhan profile response with a matching client ID and a parseable, unexpired token validity. Missing or example credentials cause no outbound Dhan request. The response never contains the client ID, access token, or raw provider response. No Dhan request is made during application startup. Without configured credentials the endpoint reports `NOT_CONFIGURED`; without successful validation it never reports live-connected.

References: [DhanHQ v2 authentication and profile documentation](https://dhanhq.co/docs/v2/authentication/).

# DhanHQ v2 market-data provider contract map

Contracts checked against DhanHQ's official v2 documentation on 2026-10-20. All credentialed calls originate in `backend/app/dhan`; no Dhan credential is returned to the frontend. The provider does not implement trading or order APIs.

| Provider method | Official contract | Request headers / body | Response validation and provenance | Limiting |
| --- | --- | --- | --- | --- |
| `get_market_quotes(instruments, kind)` | `POST /marketfeed/ltp`, `/marketfeed/ohlc`, or `/marketfeed/quote` | JSON body maps official exchange-segment enum to provider instrument IDs; `access-token`, `client-id`, JSON content type. Max 1,000 IDs. | Requires `status=success` and object `data`. Raw source `last_trade_time` is retained when returned; LTP/OHLC endpoints do not document a per-value source timestamp, so `provider_timestamp` remains null. | Quote API: 1 request/sec. Cache 1 sec, opt-in only. |
| `stream_market_data(instruments, mode="ticker")` | WebSocket `wss://api-feed.dhan.co?version=2&token=…&clientId=…&authType=2`; ticker subscribe request code 15 | Query parameters per docs. JSON batches of at most 100, maximum 5,000 instruments per connection. Only official segments and caller-supplied numeric IDs are accepted. | Parses only documented binary ticker packets. Preserves exchange epoch as `exchange_timestamp` and separately records `ingested_at`/`received_at`; disconnects propagate as provider unavailable. | One managed feed connection; reconnect backoff in worker. No WebSocket data cache. |
| `get_option_chain(underlying_scrip, underlying_segment, expiry)` | `POST /optionchain` | JSON `UnderlyingScrip`, `UnderlyingSeg`, `Expiry`; `access-token`, `client-id`. | Requires `status=success`, numeric underlying `last_price`, and strike map `oc`; provider schema is retained. Dhan does not document an observation timestamp in this response. | Shared option-chain bucket: one request every 3 sec. Cache 3 sec, opt-in only. |
| `get_expiries(underlying_scrip, underlying_segment)` | `POST /optionchain/expirylist` | JSON `UnderlyingScrip`, `UnderlyingSeg`; `access-token`, `client-id`. | Requires `status=success` and `data` as ISO `YYYY-MM-DD` dates. No source timestamp is documented. | Same option-chain bucket: one request every 3 sec. Cache 5 min, opt-in only. |
| `get_historical_candles(...)` | Daily `POST /charts/historical`; minute `POST /charts/intraday` | JSON fields from Dhan docs: `securityId`, `exchangeSegment`, `instrument`, `fromDate`, `toDate`; daily optional `expiryCode`, `oi`; intraday additionally `interval`, optional `oi`. Docs show `access-token` header and JSON content type. Minute intervals are 1, 5, 15, 25, 60 and intraday range is capped at 90 days/request. | Validates aligned OHLCV arrays and epoch `timestamp`; source timestamps remain in data and latest epoch is exposed as provenance. | Data API bucket: max 5 requests/sec. Cache 1 hour, opt-in only. |
| `get_expired_options(...)` | `POST /charts/rollingoption` | JSON includes documented segment, interval, underlying ID, instrument, expiry flag/code, strike, option type, requested fields and dates; `access-token`, JSON content type. Maximum 30-day range/request. | Requires `data.ce` and `data.pe` series (nullable sides allowed) and aligned series timestamps. Epoch source timestamps are retained and latest epoch exposed as provenance. | Data API bucket: max 5 requests/sec. Cache 1 hour, opt-in only. |

The expired-options ingestion contract and request-to-service mapping are documented in [expired-options.md](expired-options.md). The response does not expose an exact calendar expiry date; only `expiryFlag`/`expiryCode` are retained.
| `get_instrument_master(detailed=False)` | Official static CSV `https://images.dhan.co/api-data/api-scrip-master.csv` or `…-detailed.csv` | HTTP GET; public master download, no credential header. | Returns the unmodified CSV plus parsed nonempty column header and row count. CSV remains provider-owned data; its release/update timestamp is not supplied by the documented contract. No local or seeded instrument IDs are generated. | Data API bucket is conservatively used for download. Cache 1 hour, opt-in only. |

**Instrument endpoint contract gap:** Dhan also documents `GET /instrument/{exchangeSegment}` but the v2 page does not specify its response schema or headers. This provider uses the documented CSV master instead of guessing that route's contract. The CSV column guide identifies exchange, segment, display, expiry, strike, and option-type columns, while the file itself is returned intact so consumers use Dhan's actual values.

## Error handling, limits, cache, and timestamp rules

`DhanRestClient` uses endpoint-specific headers, bounded retries (at most two retries) for network/timeouts, HTTP 429, and 5xx, exponential delay, and a bounded `Retry-After`. It does not retry invalid credentials or malformed requests. `DhanErrorMapper` maps documented authentication codes (`DH-901`, 807–810), access codes (`DH-902`, 806), rate codes (`DH-904`, 805), and request/data errors without placing provider text, tokens, or response bodies in logs.

The account-scoped Redis limiter shares minimum-spacing limits between API processes. If configured Redis coordination fails, a request fails closed rather than issuing an unthrottled Dhan request. With no Redis client (local isolated use), the limiter is process-local. The official general limits are 5 Data API requests/sec and 1 Quote API request/sec; the option-chain contract's stricter 1-per-3-second limit is applied to both option-chain and expiry-list requests.

Shared Redis response caching is disabled by default. Set backend-only `DHAN_CACHE_ENABLED=true` only after the Dhan account's data-plan terms permit sharing/cache retention. Caches are populated only from a successful provider response. A cache hit includes the original `ingested_at` and original `provider_timestamp`, with `cache_hit=true`; failed/expired requests never fall back to stale cache values. Live WebSocket snapshots are not treated as REST cache responses. Where Dhan supplies no source time, provenance explicitly reports `provider_timestamp: null` rather than substituting request time.

## Opt-in live integration tests

The test module `tests/test_dhan_provider_integration.py` is skipped unless `RUN_DHAN_INTEGRATION=1`. It uses actual credentials loaded by backend settings and actual identifiers/dates supplied by environment; it has no mocked success fixtures and contains no hard-coded Dhan instrument IDs. To exercise each test, provide:

- `DHAN_CLIENT_ID` and `DHAN_ACCESS_TOKEN` as valid backend secrets.
- `DHAN_TEST_EXCHANGE_SEGMENT`, `DHAN_TEST_SECURITY_ID`, `DHAN_TEST_UNDERLYING_ID`, `DHAN_TEST_UNDERLYING_SEGMENT`, and `DHAN_TEST_OPTION_EXPIRY` from the Dhan instrument master and expiry-list API.
- `DHAN_TEST_INSTRUMENT`, `DHAN_TEST_FROM_DATE`, `DHAN_TEST_TO_DATE`, and optionally `DHAN_TEST_INTERVAL` for candles.
- `DHAN_TEST_OPTION_INSTRUMENT`, `DHAN_TEST_EXPIRY_FLAG`, `DHAN_TEST_EXPIRY_CODE`, and `DHAN_TEST_STRIKE` for rolling expired-options history.

Run explicitly from the repository root with `RUN_DHAN_INTEGRATION=1 python -m pytest tests/test_dhan_provider_integration.py`. The live WebSocket test waits for a real ticker packet and is subject to market hours and Dhan data-plan access.

Official references: [DhanHQ v2 Market Quote](https://dhanhq.co/docs/v2/market-quote/), [Live Market Feed](https://dhanhq.co/docs/v2/live-market-feed/), [Option Chain and Expiry List](https://dhanhq.co/docs/v2/option-chain/), [Historical Data](https://dhanhq.co/docs/v2/historical-data/), [Expired Options Data](https://dhanhq.co/docs/v2/expired-options-data/), [Instrument List](https://dhanhq.co/docs/v2/instruments/), and [v2 rate limits and errors](https://dhanhq.co/docs/v2/).


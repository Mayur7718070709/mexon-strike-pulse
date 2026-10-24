# DhanHQ option-chain integration

## Official contract mapping

Market Pulse calls DhanHQ v2 `POST https://api.dhan.co/v2/optionchain` from the backend. It sends JSON `UnderlyingScrip` (positive integer from the synchronized provider mapping), `UnderlyingSeg` (the synchronized Dhan segment), and `Expiry` (`YYYY-MM-DD`). The request includes server-side `access-token` and `client-id` headers. Dhan documents one unique option-chain request every three seconds; the shared provider limiter uses that interval.

The requested root must resolve to an active NSE index/equity row in the synchronized master. The expiry must also exist in the active synchronized option expiry/contracts tables. The browser never submits Dhan security IDs or segments. Unsupported roots or expiry dates return DATA UNAVAILABLE without calling Dhan.

The response follows Dhan's documented `data.last_price` and `data.oc.{strike}.{ce|pe}` structure. The adapter captures LTP, previous close, OI, previous OI, volume, IV, Greeks (delta/theta/gamma/vega), top bid/ask prices and quantities, and contract security IDs when present. Premium change and percentage change are derived only from `last_price` plus `previous_close_price`; change in OI is derived only from `oi` plus `previous_oi`. Other missing values stay null and render as “Unavailable”. No IV or Greeks are inferred.

Dhan's current documented option-chain response has no source event timestamp. Market Pulse therefore exposes `provider_timestamp: null` and a separately labeled `source_timestamp` equal to when the backend received the Dhan response. It labels a recent response as `RECENT RESPONSE`, not an exchange-time live tick. Responses older than the configured freshness threshold, or a saved real response returned after a provider failure, are labeled `DATA STALE`. No response is fabricated. Market closures are not inferred from a calendar; unchanged data ages into the stale state.

## Shared cache and duplicate suppression

Normalized chain responses are shared by underlying and expiry for three seconds, aligned to Dhan's minimum unique-request interval. Redis lock-and-cache coordination coalesces simultaneous requests across API workers. A six-hour stale copy is retained only to provide a clearly labeled stale snapshot if a later Dhan request fails. Cache timestamps are preserved from the original Dhan response receipt. If Redis coordination fails, the endpoint returns unavailable rather than pretending a request was made.

## API and UI

- `GET /api/v1/options/underlyings` returns only active roots with provider-confirmed NSE option contracts.
- `GET /api/v1/options/{underlying}/expiries` uses Dhan's documented expiry-list endpoint and returns its provenance.
- `GET /api/v1/options/{underlying}/chain?expiry=YYYY-MM-DD` returns normalized CALL / STRIKE / PUT rows and freshness provenance.

The web UI polls every five seconds and also supports manual refresh. Desktop uses a three-column table; mobile uses strike cards with CALL, STRIKE and PUT columns preserved within each card. Supabase access tokens are sent only to Market Pulse; Dhan credentials stay in the backend.

## Official documentation

- [DhanHQ v2 Option Chain](https://dhanhq.co/docs/v2/option-chain/) documents endpoint method/path, headers, request body, expiry-list request, response fields, and the three-second rate limit.
- [DhanHQ v2 Instrument List](https://dhanhq.co/docs/v2/instruments/) documents the official detailed/compact instrument-master URLs and contract fields.
- [DhanHQ v2 Annexure](https://dhanhq.co/docs/v2/annexure/) documents exchange segments and option instrument types.

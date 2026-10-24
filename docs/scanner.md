# Market Pulse 10X Scanner

The scanner is an on-demand, read-only analysis. Each request is scoped to one underlying and one explicitly selected, active expiry so the backend does not fan out an unbounded number of Dhan option-chain calls. The request uses the shared Dhan option-chain service and its Redis coordination/cache/rate limiter. Dhan documents premium, previous close, volume, OI, previous OI, IV, bid/ask and top-of-book quantities in this response. Dhan documents a unique option-chain request every three seconds. [Official DhanHQ option-chain documentation](https://dhanhq.co/docs/v2/option-chain/)

## Endpoint

`GET /api/v1/scanner/opportunities` (Supabase bearer token required)

Required: `underlying`, `expiry`.

Optional filters:

- `minimum_premium`, `maximum_premium`
- `minimum_volume`, `minimum_oi`, `minimum_change_in_oi` (absolute Dhan OI units; negative thresholds are allowed)
- `minimum_iv`, `maximum_iv` (raw Dhan IV field)
- `maximum_distance_pct` (absolute strike-to-spot distance as a percentage of spot)
- `maximum_spread_pct` (ask minus bid divided by current premium)
- `minimum_liquidity_quantity` (displayed bid quantity plus ask quantity; unavailable side sizes fail this filter)
- `max_data_age_seconds` (bounded by the server freshness policy)
- `minimum_score` (default 60), `minimum_score_coverage_pct` (default 50), `limit` (default 300), `offset` (default 0)

The scanner rejects contracts that cannot be matched by underlying, expiry, strike, option type and security ID to active synchronized Dhan instrument-master records. It excludes stale chain responses, invalid prices, negative quantities, crossed markets, missing required filter inputs, and candidates below the score or available-weight thresholds. Each returned candidate includes `included`, inclusion/exclusion reasons, and component-level explanations. `NO CLEAR OPPORTUNITY` is a valid result.

## Score normalization

Each available component is normalized to 0–100. The requested model weights are preserved. The final score is the weighted mean over only available component weights:

`total = sum(component_score × component_weight) / sum(available_component_weights)`

Missing data is never assigned zero. `available_weight_pct` shows how much of the full 100-point model contributed; by default candidates below 50 available weight points are excluded. The default minimum total score is 60. Both thresholds are request-configurable within bounds.

| Component | Weight | Normalization |
|---|---:|---|
| Direction / Momentum | 25% | Latest two Dhan 5-minute underlying closes, only when both are within 10 minutes. Side-aligned return: `clamp(50 + return_pct × 50, 0, 100)`; a ±1% move maps to 0/100. Missing recent candles means unavailable. |
| Premium Momentum | 20% | Dhan premium change versus provider previous close: `clamp(50 + percentage_change × 2.5, 0, 100)`; ±20 percentage points maps to 0/100. |
| Volume | 15% | `min(100, 100 × log10(1 + volume) / log10(10,001))`. |
| OI and change in OI | 15% | Mean of available subfactors: OI uses `min(100, 100 × log10(1 + OI) / log10(1,000,001))`; percentage change in OI uses `clamp(50 + change_pct, 0, 100)` with ±50% mapping to 0/100. If previous OI is zero, its percentage change is unavailable. |
| IV | 10% | `clamp(100 - Dhan's raw IV value, 0, 100)`. This is a ranking heuristic, not a claim that low IV guarantees an outcome. |
| Distance from strike/spot | 10% | `clamp(100 - absolute_distance_pct × 10, 0, 100)`; ATM is 100 and 10% or more away is 0. |
| Historical 10X behavior | 5% | `clamp(historical_max_multiple / 10 × 100, 0, 100)`. No valid prior mapped observations means unavailable, not zero. |

Historical behavior uses only validated Dhan rolling expired-option records for the same underlying, actual strike and option type from the previous five years. Records with a calendar expiry mapping are not needed for this rolling calculation; the Dhan rolling endpoint identifies expiry by `expiryFlag`/`expiryCode`, not a calendar expiry. Data is deduplicated by source timestamp, preferring the finest available interval. For each source day and rolling expiry/strike selection, it compares maximum high to the first stored positive open. Returned statistics identify the rolling expiry flag/code and state `calendar_expiry_confirmed: false`; they are historical behavior evidence, not the current contract's own past.

The option-chain response has no per-contract exchange event timestamp in Dhan's documented schema. The scanner therefore reports the backend response timestamp and labels its quality `RECENT RESPONSE; OPTION EVENT TIME UNAVAILABLE`; it does not claim a tick timestamp. A stale response is excluded. If a factor such as 5-minute underlying momentum cannot be validated from recent stored Dhan candles, that component is unavailable and the remaining components are renormalized.

Target levels shown (2X/3X/5X/7X/10X) are calculated from current option premium only. Scores and levels are not predictions or guarantees. The endpoint does not place or execute orders.

# Past / Present / Next / Future analysis

The analysis is built from actual Dhan observations and the Dhan-synchronized instrument/expiry map. It does not simulate, extrapolate or interpolate prices.

## API

`GET /api/v1/analysis/past-present-next-future` (Supabase bearer token required)

Required query parameters:

- `underlying`, `strike`, `option_type` (`CALL` or `PUT`)
- `past_expiry`, `present_expiry`, `next_expiry`, `future_expiry` (`YYYY-MM-DD`, strictly ascending)
- `past_from`, `past_to` (`YYYY-MM-DD`, inclusive historical date window, maximum 10 years)

The PAST phase selects the latest positive observation in the selected window from Dhan expired-options history or historical candles, joined to the exact synchronized underlying/expiry/strike/option contract. If no exact mapping or observation exists, that phase is unavailable with a reason. The historical maximum multiple is the maximum observed premium divided by the earliest observed premium for that same exact contract in the selected window; both timestamps are returned.

PRESENT, NEXT and FUTURE request the actual Dhan option chain for each explicitly selected expiry and select the exact strike and option type. The endpoint's source timestamp is the backend response-received timestamp when Dhan provides no exchange event time. Stale chain responses are returned as stale and carry age/cache metadata. NEXT and FUTURE therefore mean selected later-expiry contracts observed now at their real provider timestamps; they are cross-expiry comparisons, not forward price predictions.

The response contains all five required comparisons. Each includes source/destination premiums, change, percentage, multiple, elapsed time, both expiries and timestamps, and whether it crosses contracts. Any missing leg makes that comparison unavailable with a reason. `data_completeness` counts available, fresh, stale and missing phases. Distinct expiries remain distinct contracts even though underlying, strike and option type match.

No Dhan credentials are sent to the browser. The UI is in `frontend/app/analysis.tsx`; calculations and source selection are in `backend/app/analysis/past_present_next_future.py`.

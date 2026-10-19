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


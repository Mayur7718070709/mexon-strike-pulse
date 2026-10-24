# DhanHQ instrument-master synchronization

## Provider source and field mapping

The sync reads Dhan's official **detailed instrument CSV** (`api-scrip-master-detailed.csv`) through the existing `DhanProvider.get_instrument_master(detailed=True)` server-side client. The provider validates the CSV envelope; the synchronizer additionally requires the documented detailed fields plus the Security ID column named `SECURITY_ID`. If the master changes or a required identifier is absent, synchronization fails before the last known mapping is deactivated.

| Normalized field | Dhan detailed-master field / rule |
| --- | --- |
| symbol | `SYMBOL_NAME`, uppercase |
| display name | `DISPLAY_NAME`, falls back to `SYMBOL_NAME` |
| exchange segment | `EXCH_ID` + `SEGMENT`: NSE/I → `IDX_I`, NSE/E → `NSE_EQ`, NSE/D → `NSE_FNO` |
| security ID | `SECURITY_ID`; digits only, positive; retained as text in database |
| instrument type | `INSTRUMENT` and `INSTRUMENT_TYPE`; only matching `INDEX`, `EQUITY`, `OPTIDX`, `OPTSTK` accepted |
| underlying | `UNDERLYING_SYMBOL` + `UNDERLYING_SECURITY_ID` declared on the Dhan derivative row; use an exact matching NSE root row when present |
| expiry | `SM_EXPIRY_DATE`, ISO date prefix only |
| strike | `STRIKE_PRICE`, finite decimal |
| option type | `OPTION_TYPE`: `CE` → call, `PE` → put |
| lot/tick | `LOT_SIZE` / `TICK_SIZE`, persisted only when supplied and valid |
| expiry kind | `EXPIRY_FLAG`: `W` → weekly, `M` → monthly; otherwise regular |
| source and sync time | source `DHANHQ`; `synchronized_at` is this backend's UTC ingestion time. The CSV has no row-level exchange event timestamp, so `provider_timestamp` remains null. |

The supported index roots are NIFTY, BANKNIFTY, FINNIFTY and MIDCPNIFTY. NSE stock option contracts (`OPTSTK`) are mapped using the official underlying symbol/security-ID pair on each Dhan derivative row. When the file also contains an exact NSE root row it supplies display metadata; if that row is absent or has a different symbol alias, the provider-declared derivative identity is retained with unavailable tick/lot metadata. No security ID or exchange segment is inferred. Other exchanges, segments, instrument types and unsupported roots are excluded. MCX options are not admitted to the expired-options workflow.

Rows are deduplicated by `(exchange segment, security ID, instrument type)`. Existing mappings are never deleted: an authoritative complete sync marks absent rows inactive, while expiry dates before the UTC sync date are marked expired. Provider IDs and prior symbol/expiry/strike/type fields remain on inactive instrument rows for historical joins. The update is transactional; a failed fetch, invalid master, or unusable result rolls back and leaves the last successful mapping intact.

## API routes

- `GET /api/v1/instruments?search=&underlying=&include_inactive=&limit=&offset=` — authenticated shared provider mappings.
- `GET /api/v1/instruments/{symbol}` — authenticated mappings for a root; an unknown/unsupported root explicitly returns `available: false` and `status: UNAVAILABLE`.
- `GET /api/v1/options/{underlying}/expiries` — authenticated. Resolves the security ID and segment from the synchronized master, then calls Dhan's documented `POST /optionchain/expirylist`. It never accepts caller-supplied IDs and reports provider errors as unavailable/error responses.
- `POST /api/v1/admin/instruments/sync` — requires a Supabase JWT with server-managed `app_metadata.role = "admin"`; user metadata cannot grant this role.

The CSV source is public static instrument-reference data and is not market-live data. Expiry dates returned by the expiry endpoint carry the provider envelope provenance and cache metadata from DhanProvider. Credentials remain server-side and are not part of instrument responses.

## Migration and operation

Apply `supabase/migrations/20261020000100_dhan_instrument_master.sql` after the core Market Pulse migration. Configure the server database URL and Dhan credentials using the deployment secret manager, deploy the API, then call the sync route with an administrator Supabase session. The repository must use a server-side database role with write access; never put a service-role key in frontend configuration. No market records are seeded by the migration.

## Official documentation

- [DhanHQ v2 Instrument List](https://dhanhq.co/docs/v2/instruments/) — official detailed/compact CSV URLs and detailed column descriptions.
- [DhanHQ v2 Annexure](https://dhanhq.co/docs/v2/annexure/) — exchange segment and instrument-type enums.
- [DhanHQ v2 Option Chain](https://dhanhq.co/docs/v2/option-chain/) — documented expiry-list API contract.
- [DhanHQ v2 Expired Options Data](https://dhanhq.co/docs/v2/expired-options-data/) — supports rolling Index and Stock options and describes the `NSE_FNO` request.

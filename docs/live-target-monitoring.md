# Live target monitoring and in-app alerts

## DhanHQ v2 feed contract verified

The backend connects to `wss://api-feed.dhan.co` with the documented `version=2`, `token`, `clientId`, and `authType=2` query parameters. It subscribes to ticker packets with `RequestCode: 15`; the instrument segment and numeric security ID come only from the synchronized Dhan instrument master. Configured IDs are intersected with active Dhan master rows before subscription. The official v2 documentation permits up to five feed sockets per Dhan user, up to 5,000 instruments per socket, and up to 100 instruments per subscription message. A renewable Redis lease elects one feed-owning API process per deployment, batches initial subscriptions by 100, and caps dynamic subscriptions at 5,000 total. Dhan sends server pings every 10 seconds and closes a connection if it misses pongs for over 40 seconds; the Python WebSocket implementation responds to control pings automatically. The consumer reconnects with bounded exponential delay.

See [DhanHQ Live Market Feed v2](https://dhanhq.co/docs/v2/live-market-feed/) and its [Annexure](https://dhanhq.co/docs/v2/annexure/) for request codes, segments, disconnect behavior, and data API error codes. Checked 2026-10-21.

## Event flow

1. `run_dhan_feed` accepts decoded binary ticker packets from Dhan only. It keeps the source exchange timestamp and a separate UTC receive/ingestion timestamp, updates Redis quote keys and last-received keys, and publishes the shared market quote event.
2. Only NSE F&O contracts in `market:target:subscriptions` are copied to the durable `market:target:observations` Redis Stream. The set is rebuilt from active tracking sessions joined to active Dhan provider mappings. An ARQ worker also refreshes it every five seconds and asks the feed socket to subscribe to new entries.
3. A Redis consumer group evaluates each original feed packet in `TargetStatusService`. It requires a fresh Dhan source timestamp and a timestamp strictly newer than the stored session observation. Replayed stream entries and stale Redis quotes cannot create a target event.
4. Target level changes and `target_events` are committed first. Then `AlertEngine` idempotently writes the `user_notifications` inbox/outbox row and commits it before publishing on `market-pulse:alerts:{user_id}`.
5. The authenticated `/ws/alerts` socket derives its channel from the verified Supabase JWT subject; the client cannot choose another user's channel. `GET /api/v1/alerts` is the durable inbox. WebSocket messages are update hints; inbox rows remain available if the browser was closed or the socket was disconnected.

Failed processing leaves the stream entry pending for `XAUTOCLAIM` recovery. Notification rows use unique `(user_id,event_key)` and `(user_id,target_event_id)` constraints. A duplicate WebSocket frame is possible after a publish succeeds but the worker loses its acknowledgement; clients use the notification ID and refresh the deduplicated REST inbox.

## Deploy and operate

Run the API and the ARQ worker as separate long-lived processes against the same Supabase PostgreSQL and Redis instances. The repository Docker Compose worker already starts with `arq app.worker.WorkerSettings`. A deployment that omits the worker can still serve the API, but target levels become unverified after feed observations stop being processed and no target alerts will be delivered. `GET /api/v1/integrations/dhan/status` reports the feed's last receive time; Redis also retains `market:feed:last_received_at:{segment}:{security_id}` per contract. The worker heartbeat is `market:target:worker:last_seen` with a 30-second TTL.

Apply the versioned migration `supabase/migrations/20261022000100_realtime_target_notifications.sql` before enabling the alert endpoints. Authenticated Supabase users can read only their notification rows and update only `read_at`; inserts and event transitions are backend-owned. No service-role credential belongs in the frontend. WhatsApp and push are not configured; `NotificationChannel` and `AlertEngine` allow later channels without changing the target calculation path.

No order or trade APIs are used by this pipeline.

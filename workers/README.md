# Background workers

The Redis-backed ARQ worker runs from `backend/app/worker.py`. It registers Dhan historical candles and expired-options ingestion with bounded concurrency, a 30-minute timeout, and the REST client's bounded retries. Recovery tasks requeue lost queued/running work; per-job database claims prevent overlapping ingestion. Run the process from the backend Python environment with `arq app.worker.WorkerSettings`.

Both ingestion services require the same `DATABASE_URL`, `REDIS_URL`, `DHAN_CLIENT_ID`, and `DHAN_ACCESS_TOKEN` as the API. Each validates a real provider sample before bulk requests, persists chunk progress, and never synthesizes missing data. See [historical ingestion](../docs/historical-ingestion.md) and [expired-options history](../docs/expired-options.md) for endpoint mappings and coverage behavior.

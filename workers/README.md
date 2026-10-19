# Background worker foundation

The Redis-backed ARQ worker runs from `backend/app/worker.py` with five attempts, a 300-second job timeout and bounded concurrency. The `workers/` directory is reserved for job contracts and deployment-specific process configuration.

Historical ingestion, target tracking and alert evaluation are not registered yet. They need concrete persistence schemas, Dhan history endpoint contracts, and alert delivery rules before they can produce results. The worker must fail or retry explicitly when those dependencies are unavailable; it must never create simulated prices or report a successful ingestion without Dhan data.

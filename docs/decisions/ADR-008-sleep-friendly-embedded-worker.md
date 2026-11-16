# ADR-008: Sleep-friendly worker, embedded in the API process

- **Status:** Accepted (2026-11-16, Wisdom: keep Neon, let the DB sleep; plan to move hosting to Render)
- **Date:** 2026-11-16
- **Amends:** ADR-004 (queue design unchanged; how and where the worker waits changes)

## Context

- The database is Neon's free plan (ADR-005): **100 CU-hours/month**. Compute sleeps
  after 5 min without activity, and **is suspended until next month once the hours run
  out** (neon.com/pricing, checked 2026-11-16).
- The Phase 1 worker polled every 5 s and held a `LISTEN` connection, so the DB never
  slept: 0.25 CU × 730 h ≈ 183 CU-h/month, which ran out around day 16.
- Railway's trial ends after 30 days; Wisdom plans to move to **Render**. There, a web
  service can be free, but a separate background worker is a paid service type, and
  private networking between services isn't free.
- Neon's docs don't clearly say whether an *idle open connection* counts as activity.

## Decision

1. **The worker runs inside the API process by default** (`WORKER_MODE=embedded`),
   started and stopped by FastAPI's lifespan. One service on any host.
2. **No polling while idle.** After draining the queue, the worker asks once when the
   next job is due (or a lease expires) and then sends **nothing** to the database until
   then, capped at `WORKER_MAX_IDLE_S` (6 h, SEED S14). A test asserts zero queries while
   idle.
3. **In-process wake:** `enqueue()` registers an `after_commit` hook that wakes the
   embedded worker the moment the enqueuing transaction commits. New jobs start in
   milliseconds, with no `LISTEN` needed.
4. **Idle connections are closed:** `IdlePoolReleaser` disposes the connection pool
   after `DB_IDLE_RELEASE_S` (60 s) without use, so nothing can keep Neon awake whatever
   its rule on idle connections.
5. **Standalone mode stays available** (`python -m app.jobs.worker`,
   `WORKER_MODE=standalone` on the API). `WORKER_LISTEN=true` enables NOTIFY wake-ups for
   it, which only makes sense with an always-on database.
6. Health checks configured on the host must use **`/health`** (no DB), never
   `/health/ready` (touches the DB), or the host's checker keeps the DB awake.

## Consequences

- **Good:** the DB sleeps whenever there's no traffic. Idle cost is about 4 short
  wake-ups a day (the 6 h cap) plus whatever real traffic does. One service fewer on
  Railway; fits Render's free web service.
- **Good:** job latency improves (in-process wake vs 5 s polling).
- **Trade-off:** heavy jobs share the API's CPU and event loop. Phase 5 anomaly scoring
  must run CPU-bound code in a thread or process pool (`asyncio.to_thread`), or move
  to standalone mode if it gets heavy.
- **Trade-off:** first request after the DB sleeps waits for Neon to wake (≈ 0.5 to 5 s
  measured; connect timeout is 15 s).
- **Render free tier note:** a free web service itself spins down after a period without
  HTTP traffic. Due jobs then run when it next wakes (e.g. the next device upload).
  That's fine for the 30-day account purge, but not for anything time-critical. Revisit
  when moving to Render.
- Scheduled jobs more than 6 h away cost one wake-up per 6 h while pending.

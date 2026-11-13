# ADR-004: Postgres-backed job queue instead of Redis + Celery for v1

- **Status:** Accepted (2026-11-13, with the Phase 1 plan)
- **Date:** 2026-11-13

## Context

Trip processing, indicator computation, anomaly scoring, LLM narratives, notifications and demo replay all run in the background. The earlier `vpm-backend` scaffold planned Redis + Celery (and a Redis container in docker-compose). On Railway, Redis is another service on the bill. For a few cars the queue handles maybe a few hundred jobs a day.

Options considered:

| Option | Extra service | Notes |
|---|---|---|
| Redis + Celery / arq | Redis | Mature, but adds cost and a second system to run |
| **Procrastinate** (Postgres-backed, async) | none | Uses `LISTEN/NOTIFY` + `SKIP LOCKED`, retries, scheduled/periodic tasks, async-native |
| Home-grown `jobs` table | none | About 150 lines; we own retries, backoff, scheduling, stuck-job recovery |

## Decision

- Use a **Postgres-backed queue** on the same database. Recommendation: a **small home-grown `jobs` table** in Phase 1, using `SELECT ... FOR UPDATE SKIP LOCKED`. If periodic scheduling gets fiddly in Phase 2, switch to Procrastinate (open question for Wisdom, see the Phase 1 plan).
  - *Plain English: `FOR UPDATE SKIP LOCKED` lets several workers each grab "the next unclaimed job" from a table at the same time without ever taking the same one. A row that another worker has locked is simply skipped.*
- All business code uses one interface, `app.jobs.enqueue(job_name, payload, run_at=None, dedupe_key=None)`, and registers handlers with `@job("trip.process")`. Nothing outside `app/jobs/` knows what the backend is, so Celery could come back later without touching the logic.
- The worker runs as a **second process in the same Railway service** at first (or a second service on the same image if the process manager gets awkward; see the cost note in the Phase 1 plan).
- `Stop safely` critical packets do **not** wait in the queue. The ingestion endpoint handles them inline and enqueues the follow-up work (PRD 9: push within 30 s).

## Consequences

- **Good:** no Redis service, and enqueueing happens in the same transaction as the data write, so a job can never point at rows that were rolled back.
- **Good:** jobs can be inspected with SQL, which suits debugging and the admin page.
- **Cost:** polling plus `LISTEN/NOTIFY` adds a little load to the database. That's negligible at v1 scale.
- **Watch:** if job volume passes about 50 jobs/s or the database becomes the bottleneck, move to Redis + a real broker behind the same interface.
- No Redis, Celery or arq dependency in v1 (PRD 12).

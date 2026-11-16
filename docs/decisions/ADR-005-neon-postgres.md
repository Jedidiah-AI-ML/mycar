# ADR-005: Host Postgres on Neon instead of Railway

- **Status:** Accepted (2026-11-16, Wisdom's choice during the Phase 1 deploy)
- **Date:** 2026-11-16
- **Refines:** ADR-003 (still plain Postgres; only the host changes)

## Context

ADR-003 assumed Railway's managed Postgres, next to the api and worker. During the
Phase 1 deploy Wisdom chose Neon (serverless Postgres) instead, which takes the database
off the Railway bill and has a free tier.

## Decision

- `DATABASE_URL` points to a Neon project, using the **direct (non-pooled)** connection
  string. Neon's pooler runs PgBouncer in transaction mode, which breaks the worker's
  `LISTEN/NOTIFY` wake-up (ADR-004) and asyncpg's prepared-statement cache.
- The app accepts Neon's string as pasted: `sslmode` becomes asyncpg's `ssl`, and other
  libpq-only parameters (`channel_binding`, `options`, ...) are dropped, because asyncpg
  raises `TypeError` on unknown ones (`app/core/config.py`).
- DB connect timeout is 15 s: a Neon free-tier database waking from idle took about
  5 s in testing.
- Railway's `api` runs `alembic upgrade head` as its pre-deploy step; `worker` doesn't.

## Consequences

- **Good:** one fewer Railway service (about $1.5 to 3/month less). Neon also adds
  branching, which suits testing migrations later.
- **Latency:** the current Neon project is in `us-east-2` (Ohio) while Railway is in EU
  West. Every query crosses the Atlantic. **Open:** recreate the project in AWS
  `eu-central-1` (Frankfurt) before real data arrives; a Neon project's region can't be
  changed afterwards.
- **Free-tier compute:** the worker polls and holds a `LISTEN` connection, so the
  database never scales to zero. That will probably use up the free tier's monthly
  compute allowance. **Watch** Neon's usage page in the first week. If needed, make the
  worker poll less often or only on a schedule (a change behind `app/jobs/`, nothing
  else).
- **Cold starts:** the first request after an idle period can take about 5 s.

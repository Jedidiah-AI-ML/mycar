# ADR-003: Plain PostgreSQL instead of TimescaleDB for v1

- **Status:** Accepted (2026-11-13, with the Phase 1 plan)
- **Date:** 2026-11-13

## Context

OBD readings are time series. TimescaleDB is the obvious fit, but Railway's managed Postgres doesn't include the Timescale extension. Using it would mean a separate template or service (more money, more to run). The earlier `vpm-backend` scaffold had a commented-out TimescaleDB service.

Volume estimate for v1 (1 to 3 cars): the fast group at 1 Hz × 4 signals plus the slow group every 5 to 10 s × about 10 signals ≈ 5 to 6 rows/s while driving. At about 2 h of driving a day that's about 40,000 rows per car per day, or about 1.2 M rows per car per month. Even 5 cars × 12 months is about 70 M narrow rows. Plain Postgres handles that easily with the right indexes.

## Decision

- Use Railway's standard **PostgreSQL** service, one instance.
- `readings` is a **narrow, append-only table** (`vehicle_id, trip_id, ts, signal, value`) with:
  - a **BRIN index on `ts`**. *Plain English: a BRIN index stores only the min/max time for each block of the table instead of an entry per row. Rows arrive roughly in time order, so it's tiny (kilobytes, not gigabytes) and still lets a time-range query skip most of the table.*
  - a B-tree on `(vehicle_id, trip_id)` for per-trip processing.
- Summary tables (`reading_minute_summaries`, `trips`, `trip_indicators`) are written by the worker. The UI reads those, never raw rows.
- Keep the schema **Timescale-compatible**: `ts timestamptz NOT NULL`, no unique constraint that leaves out `ts`, and no foreign keys *into* `readings`. Then `create_hypertable('readings', 'ts')` is a small step later.
- Retention: raw readings older than a configurable window (default 180 days) can be deleted once summaries exist. Not built in Phase 1; listed as a Phase 2+ job.

## Consequences

- **Good:** one database service, the cheapest Railway setup. Standard tooling (Alembic, psql, pg_dump).
- **Cost:** no automatic compression or continuous aggregates. We write summaries ourselves in the worker.
- **Watch:** if the table passes about 200 M rows or query times grow, consider declarative monthly partitioning first, then Timescale.
- No TimescaleDB image, extension or dependency in v1 (PRD 12).

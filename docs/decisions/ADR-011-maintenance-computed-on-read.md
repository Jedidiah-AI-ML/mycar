# ADR-011: Maintenance forecasts are computed on read, never stored

- **Status:** Accepted (Phase 3, approved plan)
- **Date:** 2026-11-21

## Context

Layer 1 tells an owner when each of 10 service items is due ("oil in ~1,200 km, around
18 Dec"). A forecast depends on the service records, the odometer estimate (every trip),
the km/day of the last 30 days, the wear multipliers and today's date. So it changes
every time the car drives, a record is added, or simply a day passes.

The database is Neon's free plan, which must be allowed to sleep (ADR-005, ADR-008).

## Decision

`GET /vehicles/{id}/maintenance` computes every forecast when it's asked for, from the
stored facts (records, trips, odometer readings) and the config, using pure functions
(`app/services/maintenance.py`, `usage.py`, `service_rules.py`). Nothing derived is stored.

## Why

- **Never stale:** a stored forecast would need recomputing on every trip, every record
  and every midnight. Missing any one of those triggers would show a wrong due date.
- **No timer:** a daily "refresh forecasts" job would wake the database every day for
  nothing (ADR-008). Here the database only works when someone looks.
- **Cheap:** one car has about 10 items and at most a few hundred trips in the 30-day
  window. The read is a handful of small queries plus arithmetic.
- **Testable:** the engine is pure, so the hand-calculated golden cases test exactly the
  code the API runs.

## Consequences

- "Service due" **alerts** (Phase 6) can't fire off a stored row changing. They will run
  the same functions inside the trip pipeline (when a car has just driven, so the
  database is awake anyway) and on the weekly report.
- If many cars or a fleet view make reads slow, a per-vehicle cache keyed by the latest
  trip/record id is the next step, not stored forecasts.

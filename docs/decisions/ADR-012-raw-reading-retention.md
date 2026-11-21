# ADR-012: Raw readings kept 180 days; per-minute summaries kept for good

- **Status:** Accepted (Phase 3, decision Q3, 2026-11-20)
- **Date:** 2026-11-21

## Context

Raw readings are about 100 MB per car-month (Phase 2 estimate). Neon's free plan has
1 GB of storage, and writes are blocked when it's full, so one car would fill it in
about 10 months. Layer 3 (Phase 5) needs long histories for baselines and trends, but
it doesn't need every 1 Hz sample from last year.

## Decision

- Each `trip.summarise` (Phase 3 step 4) writes **per-minute summaries**: one row per
  trip, signal and minute, with count, mean, min and max (`reading_minutes`). That's about
  5–8 % of the raw size.
- In the same job, it deletes this car's **raw readings and positions** that are more than
  **180 days** (SEED S9) older than the car's **own newest trip**.
- Per-minute summaries, trip summaries, DTCs and Mode 06 results are kept.

## Why

- **Counted from the car's own newest trip, not from today:** a car that sits parked for
  six months loses nothing. Simulated data with old timestamps isn't wiped by a run today.
- **Inside the pipeline, not on a timer:** no extra database wake-ups (ADR-008). It runs
  when a car has just driven, which is exactly when new data arrives.
- **Positions too:** location history is personal data (NDPA, PRD 10). Keeping it no
  longer than the raw readings is the privacy-friendly default.

## Consequences

- One car settles at about 600 MB of raw data plus slowly growing summaries: inside the
  free 1 GB for a single car. More cars need a paid plan, or a shorter retention.
- Anything that needs 1 Hz detail (e.g. re-running a new indicator over old trips) works
  for the last 180 days only; older history is per-minute.
- Re-summarising an old trip after its raw rows are gone keeps the existing minute rows
  (upsert, never delete).

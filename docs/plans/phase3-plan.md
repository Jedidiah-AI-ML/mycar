# Phase 3 plan: Layer 1 maintenance (APPROVED 2026-11-20)

**Decisions (Wisdom, 2026-11-20):** all five open questions approved as recommended:
- **Q1:** unknown oil type → the shorter mineral/semi-synthetic interval (5,000 km / 3 months)
- **Q2:** pre-install service history asked at onboarding; blanks show `unknown`
- **Q3:** raw readings kept 180 days; per-minute summaries indefinitely (ADR-012)
- **Q4:** Lagos prior (~30 km/day) for a car's first 7 days of data
- **Q5:** `GET /vehicles/{id}/trips` included now

**PRD 13, Phase 3:** distance integration, service rules (seed config), forecasts, service
records, wear multipliers.
**Exit gate:** forecasts match hand-calculated cases; edge cases (gaps, odometer mismatch)
are tested.
**Builds on:** Phase 2 (live). Trips are assembled from batches; readings are stored at
1 Hz (fast group) and every 7.5 s (slow group). No hardware needed.
**Carried over from Phase 2:** the raw-readings retention job (Neon's free 1 GB fills at
about 10 car-months; the Phase 2 plan moved it here "at the latest").

---

## What Phase 3 builds

```
ml/notebooks/02_distance_and_usage.ipynb   VED + simulator: how to measure distance through
        │                                   gaps; which km/day forecast is most accurate
        ▼  numbers -> config (SEED)
backend/app/config/service_rules.yaml      10 service items, Nigeria severe-service profile
backend/app/config/wear.yaml                wear multipliers (harsh braking, heavy idle)
        │
migration 0004: trip summary columns, reading_minutes, service_items/rules/records,
                odometer_readings, vehicles.oil_type
        │
pipeline:  trip.assemble ──► trip.summarise (distance, idle share, harsh counts,
                                              per-minute summaries, old raw rows dropped)
        │
app/services/usage.py        odometer estimate, km/day, odometer-mismatch check
app/services/maintenance.py  forecast per item: due in km, due date, status   (pure functions)
        │
API: GET  /vehicles/{id}/maintenance            (viewer+)
     POST/GET/DELETE /vehicles/{id}/service-records   (manager+ to write)
     POST /vehicles/{id}/odometer               (manager+)
     GET  /vehicles/{id}/trips                  (viewer+)
```

Not in Phase 3 (later phases, per PRD 13):
- "service due" alerts and push (Phase 6)
- the maintenance UI (Phase 7)
- receipt-photo parsing (a PRD stretch goal; not planned)

## Techniques introduced (plain English)

- **Trapezoidal integration.** Distance is the area under the speed-vs-time curve. Between two speed samples we assume speed changed in a straight line, so each slice adds `(v1 + v2) / 2 × Δt`. At 1 Hz this is accurate to well under 1 %.
- **Gap handling.** If samples are missing for a few seconds (a dropped PID read), we bridge the straight line across the hole. If a hole is long (a lost batch, a coverage gap), bridging could invent or lose kilometres, so we use the GNSS track for that stretch if we have one; otherwise we count the hole as unknown and report it. The notebook picks the cut-off.
- **Haversine distance.** The great-circle distance between two GPS points, using the Earth's radius. Summing it along the track gives a second, independent distance to cross-check the speed integral.
- **Trailing-window rate.** "How fast is this car using up its service interval?" = km driven in the last 30 days ÷ 30. It adapts when usage changes and ignores old history. A new car with a short history falls back to a prior (a typical Lagos km/day) until it has a week of data.
- **"Whichever comes first."** Each service item has a km interval and a time interval. We forecast the date each would be reached and take the earlier one, the way a service book does.
- **Wear multipliers.** Hard driving wears some parts faster. A multiplier below 1 shortens the interval (e.g. ×0.85 for brakes with many harsh stops). The multipliers are conservative, capped, and documented in config, so they can be checked and changed without code.
- **Per-minute summaries and retention.** Each minute, each signal: count, mean, min, max. That's about 60× smaller than the raw readings, and enough for trends and the UI. Raw readings older than N days can then be deleted to stay inside Neon's free storage, without losing the history.

## Steps (dependency order, each with its gate)

### Step 1: Notebook `ml/notebooks/02_distance_and_usage.ipynb` (notebook-first, PRD 0.5)
| Section | What it answers |
|---|---|
| 1. Distance on VED | Trapezoidal speed integral vs GPS haversine distance per trip (petrol cars): error distribution, and how sampling rate changes it |
| 2. Gaps | Remove spans of 2 s to 10 min from real VED trips; compare bridging, GNSS fill and "unknown". Output: the bridging cut-off, and the GNSS-vs-speed disagreement that should flag a trip |
| 3. Simulator check | The same on simulated Lagos trips with outages: error vs the simulator's true distance |
| 4. km/day forecasting | On VED per-vehicle daily km and a 120-day simulated Lagos series: trailing 7/30/60-day mean, median and EWMA vs a trivial baseline (global average). Metric: error in the predicted date that 5,000 km is reached |
| 5. Odometer mismatch | How far can an integrated estimate drift over months (the speed sensor's tyre-size error is ±2–4 %)? Sets the mismatch threshold |

Output: `ml/reports/phase3_distance.md` and the numbers in `wear.yaml` / settings, all marked SEED.

**Gate:** the notebook runs top to bottom (`nbconvert --execute`); the report states each chosen threshold with its evidence and a trivial baseline.

✅ **Done 2026-11-20** (`ml/reports/phase3_distance.md` + `.yaml`). Measured:

**Distance**
- Integrating 1 Hz speed is within ±0.1 % of the simulator's true distance.
- VED GPS and speed agree to a median of +0.7 %. Per-car speed-sensor bias ranges from −1.6 % to +3.3 % (p5–p95).
- Polling every 5 s instead of 1 s would add only 0.24 % error (median).

**Gap policy** (whichever method measured better at each hole length; the cut-offs are SEED M4):
- Holes up to 10 s: bridge them (0.4–3.3 % error).
- Longer holes with GPS: GPS chord × detour factor (3.6–11 %).
- Longer holes without GPS: bridge up to 120 s, then the trip's average speed beyond that. The trip is marked *partial* once a hole is over 20 s.
- Trips where GPS and speed disagree by more than 13 % are flagged.

**Odometer mismatch (M5):** flag when the difference is more than max(500 km, 5 % of km since the last anchor).

**km/day:**
- On VED, the trailing 60-day mean (45.5 % error) beat the 30-day mean (51.6 %) and the baseline (57.5 %). The simulator can't separate the methods.
- **Trailing 30-day mean: Wisdom's decision (2026-11-20)**, as the PRD specifies. It reacts faster to changes in driving; re-check against 60 days on field data (Phase 9).
- Lagos prior: 30 km/day (M6), weighted as 7 days of data.

### Step 2: Service rules and wear config (SEED)
| File | Purpose |
|---|---|
| `backend/app/config/service_rules.yaml` | The 10 PRD items (engine oil + filter, tyre rotation, brake pad inspection, engine air filter, cabin filter, spark plugs, coolant, transmission fluid, battery check, wheel alignment check). Each has km and month intervals, a **Nigeria severe-service default profile**, oil variants (mineral/semi-synthetic 5,000 km / 3 months; full synthetic 7,500 km / 6 months), and optional per make/model/engine overrides. **All SEED** |
| `backend/app/config/wear.yaml` | Which items each multiplier affects, the bands (e.g. harsh brakes per 100 km, heavy-idle share) and a floor (never below ×0.7). **SEED** |
| `backend/app/services/service_rules.py` | Load, validate, resolve "which rule applies to this car" (most specific match wins), and sync into the DB (idempotent, like the signal catalogue) |

**Gate:** pytest: every item has both intervals; the resolver picks the most specific rule; an unknown key fails loudly; sync is idempotent.

✅ **Done 2026-11-20:** 27 tests pass (`tests/test_service_rules.py`).
- **Config:** the 10 items with intervals, the `ng_severe` profile, the oil variants, a `due_soon` threshold (10 % or 14 days, M3), and an empty `overrides` list that Wisdom fills once intervals are validated against the manuals.
- **Battery check is time-only** (6 months, per the PRD); every other item has both km and months.
- **Resolver:** the most specific matching rule wins (make+model+engine > make+model > make > default). Matching is case-insensitive, and the engine matches as a substring (`K24` matches `K24A 2.4L I4`). An unknown oil type → `mineral`, flagged as assumed (Q1).
- **Wear:**
  - A multiplier is picked from the highest band the 30-day metric is above, and shortens the **km** interval only.
  - Multipliers on one item multiply together, but never below the 0.7 floor.
  - No multiplier applies under 100 km of driving in the window.
  - The heavy-idle bands start at 50 %, above the ~40–50 % that severe service already assumes for Lagos (P2).
- **Bad config fails at load with the reason:** 12 rule mistakes and 6 wear mistakes are each tested.
- **Moved to step 3:** syncing the rules into the DB (the tables arrive with migration 0004), and its idempotency test.

### Step 3: Migration `0004_maintenance`
Tables below. **Gate:** upgrade / downgrade / upgrade round trip, `alembic check` clean, constraint tests (record dates not in the future; odometer values in range; one rule per item per scope).

✅ **Done 2026-11-20:** 22 new tests (254 backend tests in total, all pass).
- **Migration:** round trip and `alembic check` are clean.
- **Rules sync** (`service_rules.sync_to_db`): idempotent. It replaces rules and never deletes an item that service records point at.
- **"One rule per scope":** uses a `NULLS NOT DISTINCT` unique constraint, so two *default* rules can't coexist.
- **Checks written by hand:** Alembic doesn't detect CHECKs added to existing tables, so the four on `trips` and `vehicles` were added manually.
- **Dates:** "record not in the future" is checked in the API (step 7), not in the DB (a CHECK on `CURRENT_DATE` breaks restores). The DB rejects dates before 1990.
- **Additions beyond the table below:** `trips.gnss_distance_km` (the cross-check value, so a flagged trip can be inspected) and an index on `reading_minutes (vehicle_id, signal_id, minute)` for per-car trends.

### Step 4: `trip.summarise` pipeline stage
| File | Purpose |
|---|---|
| `backend/app/services/distance.py` | Trapezoidal integration with the step 1 gap policy; GNSS haversine cross-check when positions exist (with consent) |
| `backend/app/jobs/handlers/trip_summary.py` | After `trip.assemble`, for complete trips: distance and its source (`speed` / `speed+gnss` / `partial`), driving vs idle time, idle share, max speed, harsh brake/accel counts (device events + the speed derivative), per-minute summaries, then deletes this vehicle's raw readings older than the retention period. A `late_data` re-assembly re-runs it |
| `backend/app/jobs/pipeline.py` | `STAGES = ("trip.assemble", "trip.summarise")` |

**Gate:** pytest:
- distance within the notebook's error band of the simulator's true distance, including trips with lost batches
- the backend reproduces the notebook's numbers on the same trips (PRD 0.5)
- a gap longer than the cut-off is reported, not invented
- re-running is idempotent
- retention deletes only rows older than the period, and keeps the minute summaries

✅ **Done 2026-11-20:** 22 new tests (276 backend tests in total, all pass).

**Distance**
- On 3 simulated Lagos trips (fixture `tests/data/sim_trips.json`, made by `scripts/make_distance_fixture.py`), distance is within 0.3 % of the true value.
- With a 60 s batch lost: within 2 % using the GPS chord, and within 5 % without GPS (marked `partial`).
- **The backend reproduces the notebook:** notebook 02, section 8, runs `app/services/distance.py` on 595 VED trip/hole cases. Largest difference: 9.5e-10 m. A first run differed by up to 0.48 m, because the notebook used unrounded detour factors where the backend uses the published 3-decimal table. The comparison now uses the published table.
- A drift test keeps `app/config/trip_summary.yaml` equal to the notebook's decisions.

**As built**
- `app/services/distance.py` is pure Python (stdlib + PyYAML), so the notebook can import it. `app/jobs/handlers/trip_summary.py` is the new pipeline stage.
- **Harsh events:** device accelerometer events + 1 Hz speed-curve events (0.35 g brake, 0.30 g accel, M7), merged within 3 s.
- **Retention (S9, ADR-012 to write):** raw readings **and positions** more than 180 days older than *the car's own newest trip* are deleted; per-minute summaries stay. Counting from the car's own data means a parked car never loses history, and simulated runs don't get wiped.
- **Pipeline fix:** `trip.assemble` now queues the next stage whenever a closed trip gains batches, not only when its status changes. Before, a second late batch on a `late_data` trip never re-summarised.

### Step 5: Usage and odometer service
| File | Purpose |
|---|---|
| `backend/app/services/usage.py` | **Odometer estimate** = the last trusted anchor (install odometer, or a service-record odometer) + Σ trip distance since; PID 0xA6 used when the car reports it. **km/day** with the method step 1 picked, plus the cold-start prior. **Mismatch check:** a user-entered odometer that differs from the estimate by more than the threshold (step 1) is accepted but flagged (PRD: "distance since install is the trusted number") |

**Gate:** pytest: the estimate after N trips equals the anchor + their distances; partial trips are counted and flagged; the mismatch flag sets and clears at the threshold; km/day handles a new car, a car parked for two weeks, and a car with no device.

### Step 6: Forecast engine
| File | Purpose |
|---|---|
| `backend/app/services/maintenance.py` | Pure functions, no DB. Per item: the last service (record, or "before install" history) → km since and days since → interval × wear multiplier → km remaining, due date by km (via km/day), due date by time, **whichever first**, status `ok / soon / due / overdue / unknown`. "Soon" thresholds are SEED (10 % of interval or 14 days) |
| `backend/tests/data/maintenance_cases.yaml` | **Hand-calculated cases** with the working shown, for Wisdom to check: new car with no history; oil by km first; oil by time first (a car that's barely driven); full synthetic; harsh-driving multiplier; overdue; no device for 3 weeks; odometer mismatch; service recorded after install |

**Gate (the PRD exit gate):** every case in `maintenance_cases.yaml` matches to the km and to the day; property tests (more km → never "later"; a service record resets the item).

### Step 7: API + Restfox list
| File | Purpose |
|---|---|
| `backend/app/api/maintenance.py` | `GET /vehicles/{id}/maintenance` (viewer+): items with status, due in km, due date, why ("by time: oil is 3 months old"), whether a multiplier applied, odometer estimate, km/day, mismatch flag. `POST/GET/DELETE /vehicles/{id}/service-records` (manager+ to write; viewers read). `POST /vehicles/{id}/odometer` (manager+). All audited |
| `backend/app/api/trips.py` | `GET /vehicles/{id}/trips` (viewer+), paginated: start, end, distance, idle share, status |
| `backend/restfox/phase3_requests.py` | The Phase 3 request list (same runner style) |

**Gate:** pytest role matrix (viewer reads, manager writes, stranger 404) and validation; the Restfox list passes locally.

### Step 8: End-to-end gate
`scripts/phase3_gate.py`: simulate 60 days for 2 cars (one gentle, one hard Lagos driving) with service records and an odometer entry part-way; ingest over HTTP (as in Phase 2); compare `GET /maintenance` with forecasts computed by hand-checked code from the simulator's **true** distances. Report to `ml/reports/phase3_gate.md`. Then deploy, and run the Restfox list against Railway.

**Gate:**
- forecast due-km within 1 % of the truth-based forecast
- the hard-driving car's brake and oil items come due earlier, by exactly the configured multipliers
- the Restfox list passes live

## Phase 3 schema (migration 0004)

| Table / change | Key columns | Notes |
|---|---|---|
| `trips` + | `distance_km`, `distance_source`, `driving_s`, `idle_s`, `idle_share`, `max_speed_kmh`, `harsh_brake_n`, `harsh_accel_n`, `summarised_at` | Written by `trip.summarise` |
| `reading_minutes` | `vehicle_id`, `trip_id`, `minute` (timestamptz), `signal_id`, `n`, `mean`, `min`, `max` | PK `(trip_id, signal_id, minute)`; BRIN on `minute`. No FK (like readings, ADR-003) |
| `service_items` | `key` text PK (`engine_oil`, …), `name`, `description` | Synced from config |
| `service_rules` | `id`, `item_key`, `scope` (`default` / make / make+model / make+model+engine), `variant` (e.g. oil type), `interval_km`, `interval_months`, `profile` (`ng_severe`) | Synced from config; unique per item + scope + variant |
| `service_records` | `id`, `vehicle_id`, `item_key`, `done_on` (date), `odometer_km`, `before_install` bool, `notes`, `created_by` | Onboarding history uses `before_install = true` |
| `odometer_readings` | `id`, `vehicle_id`, `ts`, `km`, `source` (`user` / `service` / `obd_a6`), `estimate_km_at_entry`, `mismatch` bool | The anchor history behind the estimate |
| `vehicles` + | `oil_type` (`mineral` / `semi_synthetic` / `full_synthetic` / null = unknown) | Picks the oil rule variant |

**Storage:** `reading_minutes` adds about 5–8 % of the raw readings' size (about 1 row per signal per minute vs ~600 raw rows a minute, but each row is wider). With raw readings kept for 180 days (S9), one car settles at roughly 600 MB on Neon, inside the free 1 GB.

## Expected monthly cost

**No change.** No new services. Forecasts are computed when they're read, not on a timer (proposed ADR-011), so Phase 3 adds no scheduled jobs that would wake the Neon database (ADR-008). Retention runs inside the trip pipeline, which only runs when a car has just driven.

## Open questions for Wisdom

1. **Unknown oil type:** treat it as mineral/semi-synthetic (5,000 km / 3 months, the shorter interval) until the owner says otherwise? *(recommended; the safe side)*
2. **Service history before install:** ask for the last date and odometer per item at onboarding. Items left blank show `unknown: tell us when it was last done`, rather than guessing. *(recommended)* The alternative is to assume they were done on install day.
3. **Retention (S9):** keep raw readings 180 days and the per-minute summaries indefinitely *(recommended)*, or keep raw readings until the database reaches 80 % full?
4. **km/day cold start:** use a Lagos prior (about 30 km/day, SEED; the simulator's Lagos schedule gives 25–35) for the first 7 days of data *(recommended)*, or show "learning your usage" with no date until then?
5. **`GET /vehicles/{id}/trips`:** include it now (it makes distances checkable in Restfox, and Phase 7 needs it) *(recommended)*, or leave it to Phase 7?

## SEED values to validate

| # | Value | Where | Why unsure |
|---|---|---|---|
| M1 | Service intervals for the 10 items, Nigeria severe-service profile (PRD defaults: oil 5,000 km / 3 months mineral/semi, 7,500 km / 6 months full synthetic; tyre rotation 10,000 km; brake inspection 10,000 km; air filter 10,000 km; cabin filter 15,000 km; battery check 6 months; alignment 10,000 km). Spark plugs, coolant and transmission fluid: from general knowledge | `service_rules.yaml` | To be checked against the owner's manuals / severe-service schedules for the five cars |
| M2 | Wear multipliers and bands (harsh brakes per 100 km, heavy-idle share), floor ×0.7 | `wear.yaml` | No validated source; deliberately conservative |
| M3 | "Soon" thresholds: 10 % of interval or 14 days | `maintenance.py` | UX choice |
| M4 | Gap-bridging cut-off and GNSS disagreement flag | settings (from step 1) | Set from the notebook |
| M5 | Odometer mismatch threshold | settings (from step 1) | Set from the notebook |
| M6 | Cold-start km/day prior (~30 km/day) | settings | No Lagos usage data until the field test |
| M7 | Harsh event thresholds on the server side (speed derivative) | settings | Device side is P-values; to be matched with real data in Phase 8 |
| S9 | Raw-reading retention: 180 days | settings | Storage vs history trade-off (Q3) |

## New ADRs in this phase

- **ADR-011: Maintenance forecasts computed on read.** No stored forecasts, no daily timer: the database can sleep, and a forecast is never stale.
- **ADR-012: Raw-reading retention with per-minute summaries** (after Q3 is decided).

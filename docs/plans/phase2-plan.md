# Phase 2 plan: Simulator + device gateway (APPROVED 2026-11-16)

**Decisions (Wisdom, 2026-11-16):**
- **Q0: stay on Neon** and let the database sleep. Hosting will move from Railway to
  Render later, so the DB must not live on the app host. Done as **step 0 = ADR-008**: the
  worker is embedded in the API, sends no queries while idle, wakes in-process on commit,
  and idle pool connections are closed after 60 s. Railway's `worker` service is deleted.
- **Q1 to Q8:** approved as recommended:
  - uv workspace for the simulator
  - our own payload v1
  - unclaimed data acked and discarded
  - GNSS only with consent
  - no raw payload storage
  - full gate locally, 1 car-day live
  - ELM327 output in Phase 8
  - `readings.value` as `real`
- **Consequence for storage:** Neon free = 1 GB, which is about 10 car-months of readings.
  The raw-reading retention job (S9) moves into Phase 3 at the latest.


**Exit gate (PRD 13):** a simulated week for 5 cars is ingested with **zero duplicate rows**,
replaying it is **idempotent**, and **late / out-of-order batches** are handled.
**Builds on:** Phase 1 (live on Railway + Neon). No hardware needed.

---

## 0. Decide first: the database host (affects Phase 1 too)

Neon's Free plan (checked 2026-11-16 on neon.com/pricing): **100 CU-hours per month**,
1 GB storage, compute sleeps after 5 min idle. **When the CU-hours run out, compute is
suspended until next month**, which means AutoCare is down.

Our worker polls the jobs table every 5 s and holds a `LISTEN` connection, so the
database never sleeps. The smallest compute is 0.25 CU, so 0.25 CU × 730 h ≈ **183 CU-h a
month**. The allowance runs out around **day 16**, and has been draining since the
deploy. *Check Neon → Usage today.*

| Option | Monthly cost | Effort | Risk |
|---|---|---|---|
| **A. Move back to Railway Postgres** (ADR-003 as written) | ~$1.5 to 3 (inside the $5 Hobby credit, so likely **$0 extra**) | Small: new DB, change `DATABASE_URL`, redeploy | None from limits; always on |
| B. Stay on Neon free; make the worker let the DB sleep: no polling when idle, the API wakes the worker over Railway's private network, timers for scheduled jobs | $0 | Medium: about a day, plus a wake endpoint on the worker | 1 to 5 s cold start on the first request after idle; the CU cap is still a cliff if traffic grows |
| C. Neon paid (Launch) | Usage-based, likely more than A for an always-on DB | Small | Bill grows with uptime |

**Recommendation: A.** It's the cheapest *reliable* option. The Railway credit already
covers it, and there's no cap that can switch the product off mid-month. B is a nice
engineering exercise but adds moving parts for about $2. If you choose A, ADR-005
becomes "Superseded" and the Neon project can be deleted. Either way it's done
**before** step 6, so the bulk data lands in the final database.

---

## What Phase 2 builds

```
simulator/  (new package)                          backend/
  profiles  ── 5 cars (SEED)                         app/gateway/
  cycles    ── Lagos traffic, urban, expressway,       contract.py  (payload v1, Pydantic)
               short trips                             adapters/freematics_one_plus.py
  physics   ── speed→gear→RPM, MAF, warm-up,           registry.py
               voltage, trims, O2                    app/api/devices.py   (provision, claim, status)
  faults    ── 9 gradual faults + ground truth       app/api/ingest.py    (batches, critical)
  device    ── polling, trips, store-and-forward,    app/jobs/handlers/trips.py (trip.assemble)
               outages, retries, clock drift   ──►   migration 0002 (devices … readings)
  outputs/freematics.py ── HTTPS gzip JSON  ──────►  POST /ingest/freematics_one_plus/batches
```

Not in Phase 2 (later phases, per PRD 13):
- distance integration and service forecasts (Phase 3)
- DTC interpretation (Phase 4)
- health indicators and Mode 06 scoring (Phase 5)
- alerts and push (Phase 6)
- demo replay and the UI, including the Quick Health Scan button (Phase 7)
- the virtual ELM327 serial output (Phase 8, where python-OBD parsing matters)

Phase 2 **stores** DTC, Mode 06 and VIN records as they arrive; it doesn't interpret them.

## Techniques introduced (plain English)

- **Store-and-forward.** The device writes every reading to its own flash first and uploads in batches when it has signal. If the network drops, nothing is lost; the batches just arrive later, possibly out of order.
- **Idempotent ingestion.** Every batch has a unique key `(device_id, boot, trip, seq)`. The server records the key in the same transaction as the rows. A retried batch hits the existing key and is acknowledged without inserting anything, so retries can never create duplicates.
- **Clock-skew correction.** Device clocks drift and reset. Each batch carries the device's clock at send time; the server compares it with its own receive time (and GNSS time when there's a fix) and shifts sample times by the measured offset if it's larger than a threshold.
- **COPY bulk insert.** Postgres's `COPY` loads thousands of rows in one round trip, about 10 to 50 times faster than row-by-row `INSERT`. It matters with about 6 readings per second per car.
- **First-order thermal model (simulator).** Coolant temperature approaches the thermostat set-point exponentially, `dT/dt = (T_target − T) / τ`. τ depends on load and ambient temperature, so heat, idling and a stuck-open thermostat (lower T_target) show up realistically.
- **Debounced jobs.** Trip assembly is queued with a `dedupe_key` and a short delay. A burst of 30 batches for one trip produces one assembly run, and a late batch simply re-runs it.

## Steps (dependency order, each with its gate)

### Step 1: Payload contract + signal catalogue (ADR-006)
| File | Purpose |
|---|---|
| `docs/decisions/ADR-006-device-payload-v1.md` | Wire format, auth, idempotency key, ack/command protocol, size budget |
| `backend/app/config/signals.yaml` | Canonical signals: name, unit, PID, group (fast/slow/event), valid range (SEED) |
| `backend/app/gateway/contract.py` | Pydantic models: `BatchV1`, `CriticalV1`, `AckV1`, events (trip_start/end, vin, pids_supported, dtc, mode06, crank, gnss) |
| `contracts/freematics_one_plus.v1.schema.json` | JSON Schema exported from the models; the simulator and (later) the firmware build against it |

**Gate:** pytest: models ⇄ schema round trip; the exported schema matches the committed file (drift test); a 60 s batch of typical data is under the size budget (about 4 KB gzipped, SEED).

✅ **Done 2026-11-16:** 18 tests. Typical 60 s batch = 2.26 KB gzipped (≈ 132 KB per driving hour, ≈ 8 MB/month at 2 h/day). ADR-006 written.

Sketch of a batch (columnar, so each signal name is sent once):
```json
{"v":1,"device_id":"FM1-0001","fw":"0.1.0","boot":17,"trip":42,"seq":3,
 "t0":1763290000000,"sent_at":1763290061200,"gnss_time":null,
 "s":{"rpm":[[0,812],[1000,815]],"speed":[[0,0],[1000,2]],"coolant":[[0,71]]},
 "ev":[{"t":0,"type":"trip_start","vin":"JTDBU4EE9A9123456","pids":"BE3FA813"}],
 "net":{"rssi":-71,"bytes_up":183402}}
```

### Step 2: Calibration notebook (notebook-first, PRD 0.5)
| File | Purpose |
|---|---|
| `ml/notebooks/01_simulator_calibration.ipynb` | From KIT (warm-up curves, polling cadence, MAF vs RPM/MAP), Barreto (ELM327 rates, dropout and missing-PID patterns, older-car noise; Corolla 2009 trace) and VED petrol cars (trip length and duration distributions, idle share, km/day, fuel-trim spread) |
| `simulator/profiles/calibration.yaml` | Numbers produced by the notebook, each with its source cell |
| `ml/reports/phase2_calibration.md` | Short report: each number, its source, and how far it is from Lagos conditions |

**Gate:** the notebook runs top to bottom from `DATASETS_DIR/raw` (read-only) and writes the yaml; the report lists every value with its source.

✅ **Done 2026-11-16:** the notebook runs top to bottom (about 80 s, VED rebuilt from raw). Key results:
- MAF ÷ physics formula = **1.004** per car (the airflow model holds).
- Warm-up τ ≈ **437 s**; thermostat plateau **90 °C**.
- Warm idle RPM std ≈ **86**.
- ELM327 step ≈ **8 s**; ~**0.9 dropouts/h**.

Findings:
- VED DayNum is UTC, and VED holds only part of each car's trips.
- KIT traffic tags aren't reliable.
- Barreto's P0133 trace is a 2006 VW Polo.

Separate `ml/` uv project (not a workspace member), so data-science packages stay out of the API image.

### Step 3: Simulator core
| File | Purpose |
|---|---|
| `simulator/pyproject.toml` | Separate package in a **uv workspace** with `backend` (Q1). Deps: numpy, pydantic, httpx |
| `simulator/profiles/*.yaml` | Corolla 1.8L 2009–13, Camry 2.4L 2007–11, RX350 3.5L V6, Accord 2.4L, Sienna 3.5L: displacement, cylinders, gear ratios, final drive, tyre size, idle RPM, thermostat temperature, supported PIDs (**all SEED**) |
| `simulator/cycles/` | Stochastic drive-cycle generator (segments: idle, creep, accelerate, cruise, brake) with Lagos-heavy-traffic, urban-mixed, expressway and short-trip presets |
| `simulator/physics.py` | Per-tick model: speed → gear → RPM; load; MAF (speed-density); coolant/IAT thermal model; battery voltage (resting / cranking dip / charging); STFT/LTFT with closed-loop flag; O2 switching; fuel level |
| `simulator/run.py` | `simulate(vehicle, days, seed) -> Timeline` (deterministic for a given seed) |

**Gate:** pytest:
- physical consistency: RPM/speed matches the gear ratios within 3%; MAF rises with RPM × load; coolant settles at the thermostat temperature ± 3 °C on long trips; charging voltage only with the engine running
- trip and idle distributions within the calibration tolerances from step 2
- the same seed gives byte-identical output

✅ **Done 2026-11-17:** 13 simulator tests pass (`cd simulator; uv run pytest`). Measured:
- urban cycle: 18.6 % idle and a 7.0 min median trip (VED: 19.1 %, 6.8 min)
- Lagos heavy: 46 % standing, 12.3 km/h
- warm-up τ within 35 % of KIT's
- RPM within 3 % of gear ratios when locked; MAF tracks the physics formula (r > 0.98)

One car-week simulates in under 10 s. Faults (step 4) act only through `physics.Health`.

Catalogue gained `lambda_b1s1` (PID 0x34): these cars likely use wideband A/F sensors, so `o2_b1s1_v` is unsupported in all five profiles (SEED). Simulator is a separate uv project (ADR-007).

### Step 4: Fault library + ground truth
| File | Purpose |
|---|---|
| `simulator/autocare_sim/faults.py` + `faults/faults.yaml` | Weak battery, failing alternator, stuck-open thermostat, low coolant / overheating, intake vacuum leak, dirty MAF, ageing upstream O2, ignition-coil misfire (one cylinder), catalyst degradation. Each has an onset, a degradation curve and the signal(s) it bends |
| `simulator/faults/dtc_rules.yaml` + `autocare_sim/dtc.py` | When each fault's DTC would go **pending**, then **confirmed** (e.g. P0128 for the thermostat, P0171 for the vacuum leak), **all SEED** |
| ground truth output | Per car: fault start, km/days to pending, km/days to confirmed |

**Gate:** pytest: each fault moves its target signal in the right direction and only that system; a clean car emits no DTCs over 30 simulated days; the ground-truth file matches the emitted DTC events.

✅ **Done 2026-11-17:** 35 new tests pass (48 simulator tests in total; `cd simulator; uv run pytest`). As built:
- `simulator/faults/faults.yaml`: 9 fault curves (SEED P3); `autocare_sim/faults.py` turns them into `Health` changes.
- `simulator/faults/dtc_rules.yaml`: 10 monitors (SEED P4); `autocare_sim/dtc.py` is the simulated ECU. It runs monitors on each trip's signals with two-trip pending/confirmed logic, MIL off after 3 passes, and erase after 40 warm-ups (ADR-009).
- `simulate(..., faults=[fault("vacuum_leak")])`; `Timeline.ground_truth()` / `write_ground_truth(path)`.

Measured:
- Clean cars: **0 DTCs** in 30 days on all 5 cars, with every monitor completing.
- Each fully failed fault moves only its own monitors, on an identical trip. The real side effects: a stuck-open thermostat stops the warm-engine monitors from running, and a misfire makes the O2 reading noisier.

Lead time with the default curves (seed 2, 60 days; Corolla unless noted). This is the window AutoCare has to beat:

| Fault | Starts | DTC pending → confirmed |
|---|---|---|
| Weak battery | day 5 | none (ECU doesn't diagnose it) |
| Failing alternator | day 3 | P0562: day 21 → 22 |
| Stuck-open thermostat | day 2 | P0128: day 5 → 8 |
| Low coolant | day 3 | P0217: day 30 → 31 |
| Vacuum leak (RX350) | day 4 | P0171: day 18 → 19; P0174: day 19 → 21 |
| Dirty MAF | day 3 | P0101: day 27 → 27; P0171: day 37 → 37 |
| Ageing O2 | day 3 | P0133: day 14 → 16 |
| Misfire, cylinder 2 | day 4 | P0302: day 19 → 19 |
| Catalyst | day 3 | P0420: day 33 → 33 |

Tuning during the step: vacuum leak end value 1.4 → 2.0 g/s, because the V6 at idle barely reached +25 % trim; low coolant made more severe so it actually overheats; P0133 threshold 0.006 → 0.005 for margin over healthy cars (lowest healthy value 0.0069). The misfire draws in `physics.py` are now unconditional, so clean and faulty runs share one random stream.

### Step 5: Device emulation
| File | Purpose |
|---|---|
| `simulator/device.py` | Polling scheduler (fast 1 Hz / slow 5 to 10 s, only supported PIDs), trip start/end detection, Modes 03/07/0A/06 at trip start/end, cranking-voltage capture, flash buffer with a size limit, batching (60 s or N KB, SEED), critical packet when coolant passes the critical threshold |
| `simulator/network.py` | Coverage model (outage frequency/duration, SEED), retries with duplicate sends, out-of-order delivery, clock drift and resets |
| `simulator/capabilities.py` | Switches: no Mode 06, no GNSS, no cranking capture, … |
| `simulator/outputs/freematics.py` | HTTPS client: gzip JSON, device token, reads the ack (and commands) |

**Gate:** pytest: every emitted payload validates against the step-1 schema; with outages on, the set of delivered samples equals the set produced (nothing lost); capability switches remove exactly the right fields; **data budget** printed: KB per driving hour and MB per month for 2 h/day (target under 30 MB/month, SEED).

✅ **Done 2026-11-17:** 23 new tests pass (71 simulator tests in total, about 5 min). As built:
- Code lives under `simulator/autocare_sim/` (`device.py`, `network.py`, `capabilities.py`, `outputs/freematics.py`, `outputs/memory.py`).
- SEED values (P5–P7, P13) are in `simulator/config/device.yaml`.
- Upload order and clock handling are recorded in ADR-010.

Measured, 5 cars × 7 days with Lagos coverage (outages 4–8 % of driving time):
- Every payload validates against the contract.
- **Delivered = produced**: 2,537 batches, nothing lost, with 79 duplicate sends and 69 out-of-order arrivals.
- Delay from batch start to stored: median 60 s, p99 2.8 h (dead zones), max 5.6 h.
- Each of 11 capability switches removes exactly its own data and nothing else.
- Critical packets (overheat, voltage collapse) go out in the same second, once per kind per trip.
- Remote commands (health scan, poll rates) are answered with `cmd_result`.

**Data budget:**
- Gzip body: about 172–181 KB per driving hour. That's above the 132 KB/h measured in step 1, because the trip-start events (crank trace, readiness, DTC lists) are counted here.
- With HTTPS overhead (SEED: 700 B per request, 5.5 KB per new TLS connection): 227–241 KB/h, so **13.3–14.1 MB/month at 2 h/day**. Target < 30 MB (P13).

Not modelled yet: OTA updates; parked wake-ups (the device sleeps after 10 min, and the backlog drains on the next drive).

### Step 6: Migration `0002_devices_trips_readings`
Tables below. **Gate:** upgrade / downgrade / upgrade round trip, `alembic check` clean, constraint tests (one active assignment per device and per vehicle; batch key unique).

✅ **Done 2026-11-17:** 29 new backend tests (160 in total, all pass). Includes:
- the round trip on a fresh database, with `alembic check` clean
- one active assignment per device and per vehicle
- the batch key stored once, and `ON CONFLICT DO NOTHING` telling a retry apart
- DTC code format, Mode 06 byte ids, trip checks
- `readings.value` stored as 4-byte `real`, and no foreign keys on `readings`/`positions`

As built:
- Models: `app/models/device.py` and `app/models/telemetry.py`. `readings` and `positions` are Core tables without primary keys.
- `signals` is filled by `app.gateway.signals.sync_signals()`, an idempotent upsert that ingestion will call, so a new signal needs no migration.
- Additions beyond the table above: `devices.disabled_at`, `device_commands.done_at`, `dtc_events.cleared_at` and `trip_id`, and `mode06_results.uasid` in place of `unit` (raw values, ADR-006).
- The account purge job now deletes `readings`/`positions` explicitly (they don't cascade).

**Incident:** while checking the migration I ran Alembic with the local `.env`, which points at Neon. That applied 0002 to the live database, then removed it, then applied it again. No Phase 1 data was affected: only the new, empty tables were created and dropped. The live API's readiness check then reported "migrations behind (db=0002, head=0001)" until 0002 is deployed. The fix: `app/db/alembic/env.py` now refuses a non-local database unless `ENVIRONMENT=production` or `ALEMBIC_ALLOW_REMOTE=1` (tested).

### Step 7: Device provisioning, claim, status
| File | Purpose |
|---|---|
| `backend/app/api/devices.py` | **Admin:** `POST /admin/devices` creates a device and returns its token **once** plus a claim code for the QR label. **Owner/manager:** `POST /vehicles/{id}/device` `{device_id, claim_code}`, `DELETE /vehicles/{id}/device` (unclaim). **Viewer+:** `GET /vehicles/{id}/device` (online, last seen, signal, data used this month, effective capabilities) |
| `backend/app/gateway/auth.py` | Device token = `device_id.secret`. Only `sha256(secret)` is stored (it's high-entropy, so a slow hash isn't needed), with a constant-time compare |

**Gate:** pytest: token shown once and never stored in plain text; wrong claim code → 403; a device can't be claimed by two vehicles; claim/unclaim audited; role matrix for the device routes.

✅ **Done 2026-11-19:** 21 new tests (181 backend tests in total, all pass). As built:
- **Token and claim code are shown once.** The token is stored as SHA-256 of its 256-bit secret, checked in constant time.
- **Claim codes** are 8 typeable characters (no 0/O, 1/I/L, U), about 39 bits. They're stored as an HMAC keyed with a secret derived from `JWT_SECRET`, not a plain hash, because a short code could be brute-forced from a leaked database. Changing `JWT_SECRET` invalidates claim codes that haven't been used.
- An unknown device and a wrong code get the same 403, so device IDs can't be probed. A device in another car, or a second device for a car, gets 409. Claiming again for the same car is a no-op (200).
- Device IDs are generated automatically (`FM1-0001`, …) unless the admin gives one. The QR label text is `autocare:claim:<id>:<code>`.
- `GET /vehicles/{id}/device` shows: online (seen within 5 min, P9), last seen, RSSI band (SEED), data used this month, and effective capabilities (`app/gateway/capabilities.py`). GPS is off unless the owner has location consent.
- Additions beyond the plan: deleting a car frees its device; `device.provisioned` is audited.
- Not in v1: rate limiting on claim attempts. A 39-bit code needs ~10¹¹ guesses, and each guess needs a signed-in manager of a car.

### Step 8: Gateway, adapter, ingestion
| File | Purpose |
|---|---|
| `backend/app/gateway/adapters/base.py` | `DeviceAdapter` protocol (ADR-002): `authenticate`, `parse → NormalisedBatch`, `ack`, `encode_command` |
| `backend/app/gateway/adapters/freematics_one_plus.py` | The only adapter in v1 |
| `backend/app/gateway/registry.py` | `devices.type → adapter` |
| `backend/app/api/ingest.py` | `POST /ingest/{device_type}/batches` (gzip; auth; size limit; per-device rate limit) and `POST /ingest/{device_type}/critical` |
| `backend/app/gateway/store.py` | One transaction: insert batch key (conflict → duplicate ack, no rows), clock-skew correction, `COPY` readings, GNSS rows **only with location consent**, events → `dtc_events` / `mode06_results` / `vehicles.vin, protocol, supported_pids`, update `last_seen` and data usage, enqueue `trip.assemble` (debounced) |

The ack returns `{"ok":true,"dup":false,"cmds":[...]}`. Pending `device_commands` ride along with the ack, so the device needs no push channel. A critical packet is stored and gets a priority-0 job straight away; the alert rules arrive in Phase 6.

**Gate:** pytest:
- the same batch twice → second is `dup:true` and the row count is unchanged
- batches shuffled → identical readings
- bad or expired token → 401
- unclaimed device → acked and discarded (Q3)
- no location consent → zero GNSS rows
- clock-skewed device → corrected timestamps
- oversize body → 413
- rate limit → 429

✅ **Done 2026-11-19:** 15 new tests (196 backend tests in total, all pass), covering every gate item above plus:
- DTC lists set and clear codes, and a late, older report can't reopen a code a newer one cleared (a real bug the tests caught)
- commands ride on the ack, are resent after 2 min if unconfirmed (lost ack), and are closed by `cmd_result`
- critical packets are stored and queued at priority 0, and a retry is recognised
- uncompressed JSON is also accepted; a gzip bomb (tiny body, 3 MB inside) → 413; bad gzip → 400; contract errors → 422

As built:
- **Adapter:** `app/gateway/adapters/{base,freematics_one_plus}.py`. `NormalisedBatch` / `NormalisedCritical` are the canonical form; nothing after the gateway imports adapter code.
- **Store and endpoints:** `app/gateway/store.py`, `app/api/ingest.py`, `app/gateway/ratelimit.py` (in-memory token bucket, per process: right for one replica).
- **Clock-skew correction:** the reference is GNSS time if present, else the server receive time, compared with `sent_at`; corrected only beyond 2 s (P8). This relies on the device refreshing `sent_at` on every attempt (ADR-010). The server clock is a dependency (`server_now_ms`) so tests and the step 10 run can set it.
- **Limits (P10):** 256 KB per request as sent, 2 MB decompressed, 60 requests/min per device.
- **New catalogue signal `crank_v` (id 65):** the crank trace is stored as readings (needed in Phase 5; no raw payloads are kept, Q5).
- **Migration 0003: `trips.events` (jsonb).** It holds readiness snapshots, harsh events, trip_end reason, and unknown future event types, appended atomically.
- **Job `alert.critical`:** it records the packet in the audit trail and log. Phase 6 replaces its body with the real alert rules.
- **Not stored:** a VIN never overwrites one already on the car (a mismatch is for Phase 4 to show).

**Deploy note:** steps 8 and 9 must go live together. Step 8 enqueues `trip.assemble`, whose handler is step 9; without it the worker marks those jobs dead.

### Step 9: Trip assembly + pipeline skeleton
| File | Purpose |
|---|---|
| `backend/app/jobs/handlers/trips.py` | `trip.assemble`: start/end, duration, sample counts per signal, gap list (coverage holes), completeness %, status `open` / `complete` / `late_data`. Re-runs if a late batch arrives. Then enqueues the next stage |
| `backend/app/jobs/pipeline.py` | Ordered stage list for a finished trip. Phase 2 contains only `trip.assemble`; Phases 3 to 6 append their stages here, so nothing is stubbed |

**Gate:** pytest: the assembled trip is identical regardless of batch arrival order; a late batch after `complete` re-assembles and changes only what it should; trip end without a trip_end event (device lost power) closes after a timeout (SEED 15 min).

### Step 10: Exit-gate run (local)
`scripts/phase2_gate.py`:
1. Simulate 7 days × 5 cars with outages, retries, reordering and clock drift on, plus faults on 2 cars.
2. Ingest everything over HTTP into the local Docker stack.
3. **Replay every batch again** in a different order.
4. Report to `ml/reports/phase2_gate.md`: rows produced vs stored (**must be equal**), duplicate rows (**must be 0**), trips simulated vs assembled, late batches handled, data budget per car, ingest throughput.

**Gate:** the report shows equality and 0 duplicates, and Wisdom reviews it.

### Step 11: Live smoke test + Restfox
- Restfox list for the device endpoints (`backend/restfox/phase2_requests.py`, same runner style as Phase 1).
- **Smoke test against Railway:** 1 car × 1 day only (keeps storage small), plus one replay.

**Gate:** the list passes against Railway; the smoke run reports 0 duplicates.

## Phase 2 schema (migration 0002)

| Table | Key columns | Notes |
|---|---|---|
| `devices` | `id` text PK (e.g. `FM1-0001`), `type`, `token_hash`, `claim_code_hash`, `firmware_version`, `capabilities` jsonb, `sim_iccid`, `last_seen_at`, `last_rssi`, `bytes_month`, `bytes_month_start` | `type` must be a registered adapter |
| `device_assignments` | `device_id`, `vehicle_id`, `assigned_at`, `unassigned_at` | Partial unique: one active row per device **and** per vehicle (PRD: one device per car) |
| `device_commands` | `id`, `device_id`, `type` (`health_scan`, `set_poll_rates`), `payload`, `status` (`queued`/`sent`/`done`/`failed`), `created_by`, `sent_at`, `result` jsonb | Delivered in acks |
| `device_batches` | PK `(device_id, boot, trip_seq, seq)`, `received_at`, `clock_offset_ms`, `n_samples`, `bytes`, `trip_id` | **The idempotency record.** Small; retain 90 days |
| `trips` | `id` uuid, `vehicle_id`, `device_id`, `device_boot`, `device_trip`, `started_at`, `ended_at`, `status`, `n_samples`, `completeness`, `gaps` jsonb | Unique `(device_id, device_boot, device_trip)` |
| `signals` | `id` smallint, `name`, `unit`, `pid` | Seeded from `signals.yaml` |
| `readings` | `vehicle_id` uuid, `trip_id` uuid, `ts` timestamptz, `signal_id` smallint, `value` real | Narrow; **BRIN on `ts`**, B-tree `(trip_id, signal_id, ts)`. No FKs (ADR-003). `real` = 4 bytes, enough for OBD precision (Q8) |
| `positions` | `vehicle_id`, `trip_id`, `ts`, `lat`, `lon` (double), `speed_kmh` | Kept apart from readings so location consent can switch it off and delete it on its own (PRD 10) |
| `dtc_events` | `vehicle_id`, `code`, `status` (stored/pending/permanent), `first_seen`, `last_seen`, `freeze_frame` jsonb | Interpreted in Phase 4 |
| `mode06_results` | `vehicle_id`, `trip_id`, `ts`, `mid`, `tid`, `uasid`, `value`, `min`, `max` | Raw (ADR-006); scaled and scored in Phase 5 |

**Storage estimate:** a `readings` row ≈ 50 bytes on disk plus about 30 bytes of index. That's ≈ 1.2 M rows ≈ **~100 MB per car-month** at 2 h/day. The local gate run (week × 5 cars ≈ 1.5 M rows) ≈ 130 MB. This is why the live smoke test uses 1 car-day.

## Expected monthly cost

No new services in Phase 2.
- **If Q0 = A (Railway Postgres):** about 100 MB per car-month of volume ≈ $0.015/month per car. Negligible; total stays inside the $5 Hobby credit for a few cars.
- **If Q0 = B (Neon free):** the 1 GB storage cap is reached after about 10 car-months, then writes are blocked. Needs the retention job (raw readings > 180 days, SEED S9) or a paid plan by then.
- **Device data** (Phase 8, estimated now from the simulator): about 50 to 150 KB per driving hour gzipped plus HTTPS overhead ≈ **10 to 25 MB/month** at 2 h/day. Step 5 prints the real simulator figure.

## Open questions for Wisdom

0. **Database host:** A (Railway Postgres, recommended), B (Neon free with a sleeping worker) or C (Neon paid)?
1. **Simulator packaging:** a separate `simulator/` package in a uv workspace with `backend` (recommended; the PRD layout has it at the top level), or a module inside `backend/`?
2. **Payload format:** our own JSON contract v1 (recommended; the AutoCare firmware in Phase 8 implements it), or compatibility with the stock Freematics telelogger format? Stage A bring-up uses the stock firmware against a temporary receiver, so we don't need the stock format in the gateway.
3. **Data from an unclaimed device:** ack and discard (recommended: data from before a claim belongs to nobody, and the device mustn't fill its flash), or keep it and attach it on claim?
4. **GNSS:** stored in `positions` only while location consent is active; dropped at ingest otherwise (recommended)?
5. **Raw payloads:** store nothing raw (recommended now; batch key + counts only), or keep gzip bodies for N days for firmware debugging (revisit in Phase 8)?
6. **Live gate scope:** full week × 5 cars locally only, and 1 car-day on Railway (recommended)?
7. **ELM327 virtual serial output** moves to Phase 8 (where python-OBD parsing is tested)? OK?
8. **`readings.value` as `real`** (float4, half the size of double; ~7 significant digits). OK?

## SEED values to validate

| # | Value | Where | Why unsure |
|---|---|---|---|
| P1 | The 5 vehicle profiles: displacement, gear ratios, final drive, tyre size, idle RPM, thermostat opening temperature, supported-PID sets | `simulator/profiles/*.yaml` | From general knowledge, not manufacturer data; Wisdom to check against service manuals or real scans |
| P2 | Lagos heavy-traffic cycle: idle share ~40 to 50 %, mean speed ~12 to 18 km/h, trips/day, km/day | `simulator/cycles/` | No Lagos dataset; VED/Barreto are US/Brazil |
| P3 | Fault degradation rates and onset windows (9 faults) | `simulator/faults/faults.yaml` | Plausible shapes, not measured |
| P4 | When each fault's DTC goes pending/confirmed (e.g. P0128 thermostat, P0171/P0174 lean, P0101 MAF, P0133 O2 slow response, P0301–P0306 misfire, P0420 catalyst, P0562/P0563 system voltage) | `simulator/faults/dtc_rules.yaml` | Enabling criteria vary by manufacturer |
| P5 | Poll rates: fast 1 Hz, slow 7.5 s; cranking capture rate | `simulator/config/device.yaml` | Real adapter throughput is measured in Phase 8 |
| P6 | Batch size: 60 s or 4 KB gzipped, whichever comes first | `simulator/config/device.yaml` + ADR-006 | Trade-off between data and latency |
| P7 | Outage model: frequency, duration distribution | `simulator/config/device.yaml` (network) | Real Nigerian coverage unknown until the field test |
| P8 | Clock-skew threshold before correcting: 2 s | `gateway/store.py` | Generic |
| P9 | Device "online" = seen within 5 min | `devices` status | UX choice |
| P10 | Ingest limits: 256 KB per request, 60 requests/min per device | `api/ingest.py` | Generic |
| P11 | Trip auto-close after 15 min without data | `trip.assemble` | Long Lagos idles with engine off vs a dead device |
| P12 | Critical-packet trigger in the simulator: coolant ≥ 118 °C | device emulation | The real Stop-safely rules are set in Phase 6 |
| P13 | Data-budget target: < 30 MB/month at 2 h/day | step 5 | Depends on the SIM plan chosen |

## New ADRs in this phase
- **ADR-006:** device payload contract v1 (format, auth, idempotency, acks, commands).
- **ADR-007:** simulator as a uv-workspace package (if Q1 = yes).
- **ADR-005 update:** marked "Superseded" if Q0 = A.

# Phase 5 plan: Layer 3 health indicators and anomaly detection (APPROVED 2026-11-25)

**Decisions (Wisdom, 2026-11-25):** all six open questions approved as recommended:
- **Q1:** Mode 06 O2 response and catalyst margins replace "upstream O2 switching rate"
- **Q2:** Isolation Forest is evaluated in notebook 04 and promoted only if it earns its place
- **Q3:** priors from VED (fuel trim, airflow, idle) and the simulator (the rest, lower confidence)
- **Q4:** the PRD's three targets, measured on simulated faults and VED with injected faults
- **Q5:** banded indicator states only in the DTC context
- **Q6:** lead time reported against pending and confirmed codes; the 500 km target is set against pending

**PRD 13, Phase 5:** notebooks on VED / Barreto / simulator → indicators → baselines →
scoring → evaluation report; promote to the backend; Mode 06 parsing (CAN).
**Exit gate:** the evaluation report meets the targets, or Wisdom accepts the documented
gaps; the backend reproduces the notebook's numbers.
**PRD 5 targets (v1, to tune with Wisdom):**
- detect at least **70 %** of injected gradual faults;
- median **lead time of at least 500 km** before the DTC sets;
- at most **1 false "Service soon" per 2,000 km** on clean drives.

**Builds on:**
- **Phase 2:** raw 1 Hz readings, positions, Mode 06 results (stored raw), DTCs; a simulator with 9 faults whose ground truth records when each DTC would set (pending, then confirmed).
- **Phase 3:** trip summaries (distance, idle share, harsh events).
- **Phase 4:** the DTC context has an empty `indicators` slot waiting for this phase.

**Rule from the PRD (principle 2), carried over:** rules and statistics decide; nothing
here uses the LLM. Phase 5 produces indicator values, baselines and anomaly states.
Turning those into alerts, pushes and narratives is Phase 6.

---

## What Phase 5 builds

```
ml/notebooks/03_health_indicators.ipynb   indicators per trip + context gates, on simulator
        │                                  (5 profiles), VED (real) and KIT (warm-ups);
        │                                  normal ranges and population priors
        ▼   → ml/reports/phase5_indicators.{md,yaml}
ml/notebooks/04_detection_eval.ipynb      fault-injection evaluation: baselines, robust z,
        │                                  EWMA/CUSUM drift, hysteresis, (Isolation Forest);
        │                                  detection rate, lead time, false alarms / 1,000 km
        ▼   → ml/reports/phase5_detection.{md,yaml}   ← the exit-gate report
backend/app/config/health/*.yaml          indicators, gates, priors, detector settings
        │                                  (copied from the notebooks' yaml, SEED)
migration 0006: trip_indicators, baselines, anomalies (+ Mode 06 scaled columns)
app/services/health/indicators.py         per-trip indicator values (same code path the
app/services/health/detect.py             notebooks used) → baseline → scores → states
app/services/health/mode06.py             CAN Mode 06: unit/scaling → value, limits, margin
pipeline: trip.assemble → trip.summarise → health.score → dtc.interpret
API: GET /vehicles/{id}/health, GET /vehicles/{id}/health/{indicator}   (viewer+)
```

Not in Phase 5 (later phases, per PRD 13 and 12):
- alerts, their lifecycle, push and email, the health score and weekly report (Phase 6);
- any UI (Phase 7);
- time-to-failure models (v2).

## The v1 indicators (PRD 5), and what our data can support

All are computed **per trip, from the raw readings**, only when their context gate holds.
"Warm idle" means coolant ≥ 80 °C, speed 0 and RPM < 1,100 for ≥ 30 s (all SEED).

| System | Indicator | Gate (when it's valid) | Data to develop it on |
|---|---|---|---|
| Battery | resting voltage before start; cranking minimum; charging voltage at warm idle | engine off ≥ 1 h / crank / warm idle | simulator only (no public set has device voltage) |
| Cooling | warm-up time to 80 °C, normalised by intake air temp at start; peak coolant in traffic idle; overheat events | cold start (coolant < 40 °C) / warm idle | simulator, **KIT** (real warm-ups) |
| Fuel and air | total trim (STFT + LTFT) per bank at warm idle, and at cruise; LTFT drift | closed loop, warm | simulator, **VED** (real, 264 petrol cars) |
| Airflow | MAF measured ÷ MAF expected from RPM, load and displacement | warm, steady | simulator, **VED** |
| Ignition | idle RPM stability (std. dev. at warm idle); Mode 06 misfire counts per cylinder | warm idle | simulator, VED (idle RPM only) |
| O2 / catalyst | **Mode 06** O2 response and catalyst monitor margins (see Q1) | when the ECU reports them | simulator (Mode 06 emulated) |
| Codes history | readiness monitors incomplete + few warm-ups / km since codes cleared | trip start | simulator |
| Driving stress | harsh events per 100 km, idle share, high-RPM share | every trip | already in Phase 3 trip summaries |

**One gap to decide (Q1):** the PRD's "upstream O2 switching rate at steady cruise" needs a
**narrowband** upstream sensor sampled several times a second. All five simulated cars
(and most 2007+ Toyotas) have a **wideband** air-fuel sensor upstream, and our O2 signals
are in the slow polling group (every 5–10 s). I propose using the ECU's own Mode 06 tests
for O2 response and catalyst efficiency instead.

## Techniques introduced (plain English)

- **Context gating.** An indicator is only meaningful under the right conditions. Fuel trims
  mean little on a cold engine, and charging voltage means little while cranking. Each
  indicator has a gate, and trips that fail it simply produce no value. That's not a zero,
  and not an alarm.
- **Robust z-score (median and MAD).** "How unusual is today's value for *this* car?" An
  ordinary z-score uses the mean and standard deviation, which one bad trip drags around.
  The median and the MAD (median absolute deviation, scaled by 1.4826) ignore outliers, so
  the baseline stays honest. z = (value − median) / (1.4826 × MAD).
- **Burn-in and population priors.** A new car has no history. For its first ~10 qualifying
  trips, or 2 weeks, it's compared with typical cars of the same engine family, and shown
  with lower confidence. As its own trips arrive, its own baseline takes over (a weighted
  blend, "shrinkage" towards the prior).
- **EWMA and CUSUM (drift detection).** Slow faults (a fuel trim creeping up 0.5 % a week)
  never look unusual on any single trip.
  - **EWMA** (exponentially weighted moving average) smooths the z-scores so a steady small
    shift stands out from noise.
  - **CUSUM** (cumulative sum) adds up small deviations beyond an allowance and alarms when
    the running total crosses a limit.

  Both are classic process-control tools.
- **Hysteresis.** An anomaly must persist across N trips before it's raised, and it clears
  only after sustained recovery. This stops one odd trip from flickering an alert on and off.
- **Isolation Forest (secondary, Q2).** It builds random trees that try to isolate each trip
  from the others; unusual trips get isolated in fewer splits. It can say "this trip is
  unusual overall", but per PRD 5 it can never raise anything above Watch alone.
- **Evaluation by lead time, split by vehicle.** We inject gradual faults with a known
  start, and the simulator knows when the car's own code would set. We measure how many km
  and days earlier we noticed (lead time), and how often clean cars falsely alarm. Cars used
  to tune thresholds are never used to report results. The split is by vehicle, never by
  rows of the same car.

## Steps (dependency order, each with its gate)

### Step 1: Notebook 03, indicators and normal ranges
| File | Purpose |
|---|---|
| `ml/notebooks/_build_03_health_indicators.py` → `03_health_indicators.ipynb` | Built the same way as notebooks 01–02. Computes every indicator per trip with its gate on (a) clean simulated weeks for all 5 profiles, (b) VED petrol cars for fuel trim, airflow ratio and idle stability, (c) KIT warm-ups. Shows distributions, gate coverage (share of trips with a valid value), and within-car vs between-car spread (this decides how much a car's own baseline beats the population prior). |
| `ml/health/indicators.py` | The indicator functions, written once as plain functions on arrays (time, signal values). The notebook imports them, and step 4 promotes the same code. |
| `ml/reports/phase5_indicators.{md,yaml}` | The definitions, gates and population priors (by engine family, e.g. "4-cyl 1.8–2.0 L"), plus where each number came from |

**Gate (notebook):**
- every indicator has a valid value on at least 50 % of the clean trips where its inputs exist (SEED);
- simulator vs VED distributions agree within tolerance for the shared indicators (fuel trims, MAF ratio, idle RPM spread);
- VED's real values are physically plausible (e.g. total trim within ±10 % on most warm-idle trips).

### Step 2: Notebook 04, detection and the evaluation report (the PRD gate)
| File | Purpose |
|---|---|
| `ml/notebooks/_build_04_detection_eval.py` → `04_detection_eval.ipynb` | The evaluation set:<br>- **Faults:** the 9 library faults × 5 profiles × several seeds, 60–90 simulated days each.<br>- **Clean:** matching clean cars.<br>- **Real-trace injection:** vacuum-leak and dirty-MAF ramps applied to VED cars' real fuel-trim / MAF traces.<br><br>Detector: burn-in baseline → robust z → EWMA / CUSUM → hysteresis, with Isolation Forest as a secondary signal. Thresholds are tuned on a tuning set of cars and reported on held-out cars. Compared against a **trivial baseline** (a fixed threshold on the raw value). |
| `ml/health/detect.py` | Baseline, scoring and hysteresis as plain functions (promoted in step 4) |
| `ml/reports/phase5_detection.{md,yaml}` | **The exit-gate report.** Per fault type:<br>- detection rate;<br>- lead time in km and days before the **pending** and the **confirmed** DTC;<br>- false "Watch" and "Service soon" per 1,000 km on clean cars;<br>- the trivial baseline's numbers alongside.<br><br>Plus the chosen detector settings. |

Faults with no DTC (weak battery): detection rate and days from onset to detection only.
The simulation set is cached in `DATASETS_DIR/interim/` and sized to run in under an hour.

**Gate (PRD):** the report meets the three targets, or Wisdom accepts the documented gaps.

### Step 3: Mode 06 (CAN)
| File | Purpose |
|---|---|
| `backend/app/config/health/mode06.yaml` | **SEED:** for the monitors we use (misfire per cylinder, catalyst, O2 response, EVAP, EGR, VVT where present): what each MID/TID means, and the unit/scaling formula for each unit-and-scaling ID (UASID) we use. Unknown IDs are kept raw and flagged, never guessed. |
| `backend/app/services/health/mode06.py` | Raw → scaled value, min and max, **margin to the limit** (as a share of the allowed range). Non-CAN results stay raw (PRD 5: CAN in v1, log the rest). |

**Gate:** pytest:
- the scaling of worked examples for each UASID we use;
- the margin math;
- an unknown UASID kept raw and flagged;
- the simulator's Mode 06 output parses end to end.

### Step 4: Promote to the backend (the reproduce gate)
| File | Purpose |
|---|---|
| migration `0006_health` | `trip_indicators`, `baselines`, `anomalies` (schema below) |
| `backend/app/services/health/indicators.py`, `detect.py` | The notebook's functions, promoted unchanged in logic, plus database I/O |
| `backend/app/config/health/indicators.yaml`, `detector.yaml` | Copied from the notebooks' yaml (SEED). A test checks they still match. |
| `backend/app/jobs/handlers/health_score.py` | New pipeline stage `health.score`, after `trip.summarise` and before `dtc.interpret`. It computes the trip's indicators from raw readings and Mode 06, updates the baselines, scores, and applies hysteresis. |

**Gate:**
- pytest for the indicators, gates, burn-in and hysteresis;
- **the reproduce test:** the backend, run on the notebook's fixture trips (a small saved slice of the evaluation set), gives the notebook's per-trip values and anomaly decisions (values to 1e-6, decisions exactly).

### Step 5: API, and indicators in the DTC context
| File | Purpose |
|---|---|
| `backend/app/api/health.py` | `GET /vehicles/{id}/health` (viewer+): each indicator's latest value, baseline, z-score, state (normal / watch / service soon), confidence (burn-in or prior), and the open anomalies. `GET /vehicles/{id}/health/{indicator}`: its per-trip history. |
| `app/services/dtc/build.py` | Fills the context's `indicators` slot with **banded states only** (e.g. "fuel trim bank 1 at idle: watch"). Raw values would change the cache key after every trip (Q5). |
| `backend/restfox/phase5_requests.py` | The Phase 5 request list |

**Gate:**
- pytest: the role matrix, burn-in shown as low confidence, an indicator with no valid trips shown as "not enough data";
- the DTC context hash stays stable across similar trips;
- the Restfox list passes locally.

### Step 6: End to end + live
`scripts/phase5_gate.py`:
- **Fault cars:** simulated cars with a slow fault (vacuum leak, failing alternator, stuck-open thermostat) are ingested over HTTP, and the pipeline scores every trip.
- **Clean car:** a clean car drives alongside them.
- **Checks:** anomalies appear before the simulated DTC, with lead times matching the notebook within tolerance; the clean car raises no "Service soon".

Then deploy, and run the Restfox list live.

**Gate:** the report shows the lead times and the clean car's false-alarm count; the list passes live.

## Phase 5 schema (migration 0006)

| Table | Key columns | Notes |
|---|---|---|
| `trip_indicators` | `trip_id`, `vehicle_id`, `indicator`, `value`, `n` (samples used), `gate_ok` | One row per trip per indicator that passed its gate; ~15 per trip |
| `baselines` | `vehicle_id`, `indicator`, `n_trips`, `median`, `mad`, `source` (prior / blended / own), `updated_at` | One row per car per indicator |
| `anomalies` | `id`, `vehicle_id`, `indicator`, `kind` (level / drift / unusual_trip), `state` (watch / service_soon / cleared), `opened_trip_id`, `opened_at`, `cleared_at`, `peak_z`, `evidence` jsonb | Phase 6 turns these into alerts |
| `mode06_results` (existing) | adds `value`, `min_value`, `max_value`, `unit`, `margin` (nullable) | Scaled on write; raw columns kept |

## Expected monthly cost

| Item | Estimate |
|---|---|
| Compute | a few ms per trip in the existing worker; no new service |
| Storage | ~15 small rows per trip (well under 1 MB per car-month) |
| scikit-learn in the API image (only if Isolation Forest is promoted, Q2) | ~30–40 MB bigger image; **no bill** |

**New monthly bill: none.** All evaluation runs locally.

## Open questions for Wisdom

1. **O2 indicator:** replace "upstream O2 switching rate" with the ECU's **Mode 06 O2 response and catalyst monitor margins** *(recommended: all five simulated cars have a wideband upstream sensor, and our O2 sampling is too slow for switching rate)*, or keep switching rate for narrowband cars only and poll O2 fast on them?
2. **Isolation Forest:** evaluate it in notebook 04 and **promote it only if** it catches faults the per-indicator detectors miss without adding false alarms *(recommended)*, or always ship it as the PRD's secondary signal?
3. **Population priors:**
   - **Fuel trim, airflow and idle stability:** VED, grouped by engine family.
   - **Everything else** (voltages, warm-up): the simulator, shown with lower confidence.

   *(Recommended: VED has no make or model, and no public set has device voltage.)*
4. **Targets:** keep the PRD's three targets, measured on simulated faults and on VED with injected faults *(recommended)*. Note: meeting them on simulated data says little about real cars; the real test is Phase 9.
5. **Indicators in the DTC context:** banded states only *(recommended: keeps Phase 4's cache key stable)*, or raw values?
6. **Lead time reference:** report lead time before both the **pending** and the **confirmed** code, and set the 500 km target against **pending** *(recommended: pending is when the car itself first notices, so it's the stricter test)*, or against confirmed?

## SEED values to validate

| # | Value | Where | Why unsure |
|---|---|---|---|
| H1 | "Warm idle": coolant ≥ 80 °C, speed 0, RPM < 1,100, ≥ 30 s | `config/health/indicators.yaml` | Varies by engine; a first guess |
| H2 | Cold start: coolant < 40 °C; warm-up target 80 °C | same | Thermostat opening temps differ by make |
| H3 | Resting voltage measured after ≥ 1 h engine off | same | Surface charge takes time to settle; 1 h is a compromise |
| H4 | Burn-in: 10 qualifying trips or 14 days (PRD) | `detector.yaml` | PRD default; may need to be per indicator |
| H5 | z thresholds (Watch / Service soon), EWMA weight, CUSUM allowance and limit | `detector.yaml` | Tuned in notebook 04 on simulated data |
| H6 | Hysteresis N (trips to raise) and M (trips to clear) | `detector.yaml` | Tuned in notebook 04 |
| H7 | Gate-coverage minimum: 50 % | step 1 gate | A judgement call |
| H8 | Mode 06 MID/TID meanings and UASID scaling for the monitors we use | `config/health/mode06.yaml` | From SAE J1979 knowledge; the exact standard isn't reproduced here |
| H9 | Engine-family groupings for priors | `indicators.yaml` | VED gives only engine configuration and displacement |

## New ADRs in this phase

- **ADR-014: Health detection uses robust per-car baselines, drift detectors and hysteresis.** Covers why median/MAD plus EWMA/CUSUM, why rules (not Isolation Forest) set the state, and how burn-in borrows population priors.
- **ADR-015: Indicators are computed in the pipeline from raw readings.** Raw readings are kept for 180 days (ADR-012), so a trip is always scored while its raw data exists. Minute summaries can't support idle stability or cranking minimum.

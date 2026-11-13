# AutoCare: Product Requirements Document (v1) and Build Brief for Claude Code

> **How to use this file.** Save it in the repo as `docs/PRD.md`. It replaces every earlier PRD for this project. Section 0 is addressed to Claude Code and says how to work; sections 1 to 14 are the product and technical spec.

---

## 0. Instructions to Claude Code (read first, follow throughout)

You are the engineering partner on AutoCare. The owner is Wisdom (Godswisdom Ogbonna): M.Eng. Electrical & Computer Engineering, background in automotive diagnostics and control/automation, strong in Python, FastAPI, ML and RAG. He is not a mobile developer. He builds slowly and deliberately and wants to understand every layer.

**This is a personal MVP and portfolio project, not yet a commercial product.** Cost matters: prefer the cheapest option that works, run as few hosted services as possible, and call out anything that adds a monthly bill before adding it.

**Working rules**

1. **Read this whole PRD before doing anything.** It is the source of truth. If something in the repo or in a later message conflicts with it, stop and ask; do not silently pick one.
2. **Audit before you build.** A Phase 1 scaffold already exists (FastAPI app, `GET /health` and `GET /health/ready`, multi-stage Dockerfile binding to Railway's `PORT`, Railway deployment, a mock OBD simulator, `uv` + `pyproject.toml`, Python 3.12). Inspect it, report what you found, and reuse everything that fits this PRD. Propose changes to anything that does not fit; don't rewrite working code for taste.
3. **Plan, then wait.** At the start of every phase, produce a written plan: files to create or change in dependency order, what each does, and the test gate for each step. Do not write code until Wisdom approves the plan.
4. **Test gates are mandatory.** Each step ends with a passing test (pytest for code, a Restfox-ready request list for endpoints, a notebook result for ML). Do not move to the next step on a red or skipped gate.
5. **ML goes notebook-first.** Every model, indicator and threshold is developed and evaluated in a Jupyter notebook under `ml/notebooks/` before it is promoted to production code under `backend/`. The promoted code must reproduce the notebook's numbers on the same data.
6. **Never invent automotive facts silently.** Any threshold, service interval or PID behaviour you are not certain of goes into a config file marked `# SEED - validate`, and you list it for Wisdom to check.
7. **Record decisions.** Every non-obvious architecture choice gets a short ADR in `docs/decisions/ADR-NNN-title.md` (context, decision, consequences).
8. **Explain as you go.** When you introduce a technique (robust z-scores, CUSUM, Isolation Forest, BRIN indexes, ESP32 store-and-forward), add a 3 to 6 line plain-English explanation in the plan or the notebook. Wisdom is using this project to level up in ML and embedded work.
9. **Stay in scope.** Section 12 lists what is NOT in v1. Do not build it, stub it, or add dependencies for it, even "for later". The one exception is the device-adapter interface (Section 7), which is designed so other devices can be added later.

**Kickoff task (Phase 0):** audit the repo, list what exists versus this PRD, flag conflicts, write the ADRs listed in Phase 0, and propose the Phase 1 plan. Then stop and wait.

---

## 1. Product summary

AutoCare is a **web app** that turns a single **cellular OBD-II device** into an early-warning system for a car. The device plugs into the car's OBD-II port, reads what the car's computers already know, and sends it over its own SIM straight to the AutoCare backend. No phone, no app install, no Bluetooth pairing. The owner opens a link and sees, in plain language, when something is drifting, what it probably means, how urgent it is, and when routine maintenance is due, **before** a dashboard warning light comes on.

**One-line promise:** "Know what your car needs before it breaks down."

**Why web + cellular:** browsers can't reliably talk to Bluetooth dongles (no iPhone support, nothing in the background), and asking users to install an unknown app adds friction. A cellular device removes the phone from the data path entirely, so the whole product can be a web app. It also means the owner sees the car's health even when someone else is driving.

**v1 market:** Nigeria. Private car owners first; small fleets (a handful of cars, shared access) through the same web app.

**v1 vehicles:** petrol (gasoline) internal-combustion passenger cars and SUVs that are OBD-II / EOBD compliant. No EVs, no hybrids, no diesel-specific logic.

**Stage:** personal MVP and portfolio project. One to a few test cars. Built as cheaply as possible to prove the idea works before any commercial spend.

## 2. Problem and context

- Nigerian roads carry a large share of imported used cars (Toyota Corolla, Camry, Sienna, Highlander, RAV4; Lexus RX/ES/GX; Honda Accord/CR-V; Hyundai/Kia), many from roughly 2003 to 2016.
- Operating conditions count as "severe service" almost everywhere: heat, dust, stop-and-go traffic with long idling, poor roads, variable fuel quality.
- Owners usually find out about problems when the car breaks down or a warning light comes on, when the repair is already larger. Owners can't easily judge what mechanics tell them.
- Used-car buyers face cleared fault codes and tampered odometers.
- Owners who aren't the driver (family cars, cars with a hired driver, small fleets) have no visibility into the car's condition.
- People want results with zero effort: plug in once, then just look at a link.

## 3. Users and core jobs

| User | Job to be done |
|---|---|
| **Owner-driver** | "Tell me early, in simple words, if something is going wrong and when my next service is due." |
| **Owner, not driving** (car with a driver, family car) | "Let me see the car's health and alerts from anywhere." |
| **Small fleet owner** (2 to 20 cars) | "Show me which car needs attention first." |
| **Used-car buyer** | "Scan this car and tell me if codes were recently cleared or something is hiding." |
| **Portfolio visitor / interviewer** | "Click a link and see the product working, without hardware or installs." |

Primary persona for v1 design: a non-technical owner in Lagos or Abuja with a 2008 to 2015 Toyota, any smartphone browser, intermittent mobile data.

## 4. Product principles

1. **Plain language first.** Every insight answers: what's happening, how sure we are, how urgent, what to do. No raw PIDs on the main screens.
2. **Rules decide severity; the LLM only explains.** Safety-relevant urgency ("stop safely") is set by deterministic rules, never by the LLM.
3. **Each car is compared with itself.** Anomaly detection uses a per-vehicle baseline, backed by make/model/engine population priors for cold start.
4. **Honest uncertainty.** Show confidence; never claim a car is "safe" just because nothing has been flagged. If the device can't read something on this car, say so.
5. **Zero-effort for the user.** Plug in once; everything else is automatic.
6. **Read-only on the car.** v1 never clears codes (Mode 04) or commands the vehicle (Mode 08).
7. **Cheap to run.** Minimal hosted services; data and LLM usage measured per car.

## 5. Intelligence architecture (five layers)

v1 is primarily **unsupervised and rule-based**: no labelled failure data is required to ship. Labels are collected from day one for later supervised work.

### Layer 1: Maintenance scheduling (rules + usage forecasting)

- Service items: engine oil and filter, tire rotation, brake pad inspection, engine air filter, cabin filter, spark plugs, coolant, transmission fluid, battery check, wheel alignment check.
- Each item has a km interval and a time interval; whichever comes first triggers. Intervals live in a config/DB table per make/model/engine, with a **Nigeria severe-service default profile**.
- Seed defaults (all `# SEED - validate` against manufacturer severe-service schedules): oil 5,000 km / 3 months (mineral or semi-synthetic), 7,500 km / 6 months (full synthetic); tire rotation 10,000 km; brake inspection 10,000 km; engine air filter 10,000 km (dusty); cabin filter 15,000 km; battery check every 6 months; alignment check 10,000 km.
- **Distance tracking:** integrate vehicle speed (PID 0x0D) over time per trip, handling gaps; cross-check with the device's GNSS; use odometer PID 0xA6 when supported. The odometer is entered by the user at onboarding and on each service; flag large mismatches (odometer tampering is common in imported cars, so distance since install is the trusted number).
- **Due-date forecast:** per-vehicle km/day from a trailing 30-day window gives "Oil change due in ~1,200 km, around 18 Dec at your current pace."
- **Wear multipliers (v1 simple):** harsh-braking and harsh-acceleration counts (from speed derivative and the device's accelerometer) and heavy-idle share adjust brake and oil forecasts. Multipliers are config, documented, conservative.
- Service records come from user entry. Receipt-photo parsing by LLM is a stretch goal.

### Layer 2: Fault code interpretation (DTC + LLM with RAG)

- Read stored (Mode 03), **pending (Mode 07)** and permanent (Mode 0A) codes at every trip start and end, plus freeze frame (Mode 02) for stored codes.
- `dtc_definitions` table seeded with generic SAE codes (P0xxx, P2xxx, C0xxx, B0xxx, U0xxx): code, official description, system, default severity tier, common causes, whether it is "stop safely" class. Check the licence of any source used to seed it.
- The LLM (via LiteLLM) produces the plain-language explanation, grounded in: the definition row, vehicle make/model/year/engine, freeze-frame values, related health indicators, and recent anomalies. It returns structured JSON (summary, likely causes ranked, what to do, questions to ask the mechanic). It must not invent codes or definitions not in its context.
- Pending codes get their own message: "Your car's computer noticed something once but hasn't confirmed it. The warning light is still off. Here's what to watch."
- Explanations for common codes are pre-generated offline and reviewed by Wisdom; live generation is used for rarer combinations and cached by input hash (keeps LLM cost low).

### Layer 3: Health indicators and anomaly detection (the core, unsupervised)

**Signals before the warning light:**
1. **Pending codes (Mode 07)**, as above.
2. **On-board monitor test results (Mode 06):** the ECU's own self-test values with min/max limits (misfire counts per cylinder, catalyst efficiency, O2 sensor response, EVAP, EGR, VVT where supported). Track the **margin to the limit** over time; a shrinking margin is an early warning. CAN vehicles report by Monitor ID (MID/TID with unit scaling); pre-CAN vehicles differ. Support CAN in v1; log non-CAN raw for later.
3. **Physics-based health indicators** computed per trip from live data (Mode 01) and the device's battery-voltage reading.

**v1 health indicators** (computation details and thresholds go in config, all `# SEED - validate`):

| System | Indicator | Inputs | Early sign of |
|---|---|---|---|
| Battery and charging | Resting voltage (engine off), cranking minimum voltage, charging voltage at warm idle | Device voltage reading, PID 0x42 | Weak battery, failing alternator, parasitic drain |
| Cooling | Warm-up time to operating temperature (normalised by intake air temp at start), peak coolant temp in traffic idle, overheat events | PID 0x05, 0x0F, speed | Stuck-open thermostat, low coolant, fan or radiator problems |
| Fuel and air | Total fuel trim (STFT+LTFT) per bank at warm idle vs cruise; LTFT drift over weeks | PIDs 0x06 to 0x09, RPM, load | Vacuum leak (lean at idle only), dirty MAF, weak fuel pump or injectors (lean everywhere) |
| Airflow | MAF g/s vs expected from RPM, load and displacement | PID 0x10, 0x0C, 0x04 | Dirty or failing MAF, intake leak |
| Ignition | Idle RPM stability at warm idle; Mode 06 misfire counts per cylinder | PID 0x0C, Mode 06 | Developing coil or spark plug failure |
| Oxygen sensors and catalyst | Upstream O2 switching rate at steady cruise; Mode 06 catalyst monitor margin | O2 PIDs, Mode 06 | Ageing O2 sensor, catalyst degradation |
| Codes history | Readiness monitors incomplete + low distance/warm-ups since codes cleared | PID 0x01, 0x30, 0x31 | Codes recently cleared (used-car check, or someone hid a fault) |
| Driving stress | Harsh brake/accel events per 100 km, idle share, high-RPM share | Speed, RPM, accelerometer | Faster brake, tire and oil wear |

**Detection method (v1):**
- **Burn-in baseline:** first ~10 qualifying trips or 2 weeks per vehicle (configurable). Before burn-in completes, compare to population priors for the same make/model/engine family and show lower confidence.
- **Per-indicator scoring:** robust z-score against the vehicle's rolling baseline (median and MAD), plus **CUSUM or EWMA drift detection** for slow trends (fuel trim drift, warm-up time creep, Mode 06 margin shrink).
- **Multivariate check:** Isolation Forest on per-trip indicator vectors as a secondary signal; it can raise "unusual trip" but cannot alone raise severity above "Watch".
- **Context gating:** only score an indicator under valid conditions (e.g. fuel trims only in closed loop with a warm engine; charging voltage only at warm idle).
- **Alert hysteresis:** an anomaly must persist across N trips (config) before it becomes an alert; alerts clear only after sustained recovery.

**Evaluation (required before any of this ships):**
- Build a **fault injection library** (Section 8) that adds gradual degradation to real and simulated traces, each with ground truth: fault start, and the point where the car's own DTC would set.
- Report per fault type: detection rate, **lead time** (km and days before DTC-set), and **false alarms per 1,000 km** on clean data.
- v1 targets (to tune with Wisdom): detect at least 70% of injected gradual faults with median lead time of at least 500 km before DTC-set; no more than 1 false "Service soon" alert per 2,000 km on clean drives.

### Layer 4: Narrative and alerts (LLM output, rules for urgency)

- **Severity tiers (set by rules):** `Info`, `Watch`, `Service soon`, `Stop safely`.
- `Stop safely` is deterministic only: e.g. coolant over a critical threshold or rising fast, flashing-MIL class misfire codes (P0300 series with high counts), charging voltage collapse while driving, brake/ABS C-codes marked critical in `dtc_definitions`.
- The LLM writes the health summary and each alert's explanation in simple English, at about a 12-year-old reading level, from structured inputs. It returns JSON validated by Pydantic; on validation failure, fall back to a template.
- **Weekly health report:** overall health score (transparent formula from indicator states, not an LLM opinion), what changed, upcoming services, one tip.
- Every LLM call is logged with model, prompt version, input hash, tokens and cost. Prompts are versioned files under `backend/app/llm/prompts/`. Default to a low-cost model.
- Delivery: in-app feed, **web push** (the web app is an installable PWA, which works on Android and on iPhone when added to the home screen), and **email** as a fallback. All free channels. WhatsApp/SMS are out of v1.
- Limitation to state in the UI: an urgent alert reaches the driver only if the driver has the web app installed with notifications on, or the owner relays it. There is no in-car screen or buzzer in v1.

### Layer 5: Time-to-failure (deferred to v2)

- Not built in v1. v1 must **collect the labels** it will need:
  - After any alert, ask "Did you take the car to a mechanic? What did they fix?" (structured choices + free text).
  - Service and repair records linked to the alerts and indicator history before them.
- v2 approach: survival analysis (censored data) rather than plain regression; prototype on the public SCANIA Component X dataset, then train on AutoCare's own labelled data.

## 6. v1 feature list (all in the web app)

1. Account, onboarding and consent (NDPA-compliant, see Section 10).
2. Add vehicle: VIN read by the device (Mode 09, fallback manual), make/model/year/engine decode, odometer at install, last-service details per item.
3. **Claim a device:** enter the device ID (or scan its QR label) to link it to the vehicle; see device status (online, last seen, signal, data used this month).
4. Automatic trip detection and data capture by the device (engine on to engine off), store-and-forward when out of coverage.
5. **Quick Health Scan:** a button in the web app sends a remote command to the device to run an on-demand scan: all codes (stored, pending, permanent), readiness monitors, codes-recently-cleared check, battery/charging snapshot, Mode 06 summary. Doubles as a used-car check.
6. Vehicle overview: health score, a few plain-word status tiles (engine temperature normal/high, battery normal/low, etc.), latest trip, live status while driving (refreshing every few seconds is enough).
7. Maintenance schedule with due-in-km and due-date forecast; mark service done.
8. Health insights feed and alerts with severity tiers.
9. Fault code explanations (stored and pending).
10. Weekly health report.
11. **Shared access:** owner invites others with roles `viewer` or `manager`; fleet view lists vehicles sorted by urgency.
12. Repair feedback capture (labels for Layer 5).
13. **Public demo mode:** a "Try the demo" link that opens a read-only demo account with 2 to 3 simulated cars (one healthy, one with a developing fault, one with a pending code). Data comes from replaying stored simulator output on a schedule, so there is no always-on process to pay for. This is what portfolio visitors and interviewers see.

## 7. System architecture

```
[Car OBD-II port]
   └─ Freematics ONE+ (ESP32 + LTE modem + GNSS + motion sensor)
      AutoCare firmware: polling scheduler, trip detection,
      flash/SD buffer, batch upload over the device's own SIM,
      immediate "critical" packet, remote commands, OTA updates
         │  HTTPS
         ▼
   Device Gateway (FastAPI ingestion; one adapter per device type;
                   every payload normalised to the canonical reading schema)
         │
   FastAPI backend (Railway, one service)
   ├─ PostgreSQL (one service; Timescale optional later)
   ├─ Background worker (Postgres-backed job queue):
   │     trip processing → health indicators → anomaly scoring
   │     → rules/severity → LLM narrative → alerts
   ├─ LiteLLM (provider-agnostic LLM calls, low-cost default model)
   ├─ Web push (VAPID) + email
   └─ REST API
         │
   Web app (Next.js PWA on Vercel): owner, fleet, demo
[Dev/test] OBD simulator emulates the device end to end
```

**Stack:** Python 3.12, `uv`, FastAPI, Pydantic v2, async SQLAlchemy 2 + Alembic, PostgreSQL, scikit-learn, pandas, LiteLLM, pytest, Docker, Railway (backend; prefer an EU region for latency to Nigeria), Next.js on Vercel (web), Restfox (manual API testing), Jupyter (ML), Arduino/PlatformIO toolchain (firmware).

**Cost-driven stack changes (ADRs required in Phase 0):**
- **Plain PostgreSQL instead of TimescaleDB for v1.** One to a few cars produce a few million rows a month, which plain Postgres handles well with a time-ordered table, a BRIN index on time and simple summary tables. This removes a separate paid service. Keep the schema Timescale-compatible so migrating later is a small step.
- **Postgres-backed job queue instead of Redis + Celery for v1** (e.g. Procrastinate, or a `jobs` table using `SELECT ... FOR UPDATE SKIP LOCKED`). Removes the Redis service. Hide it behind a small `enqueue(job, payload)` interface so Celery can return later without touching business logic.
- Target: backend + database + worker on the smallest Railway footprint; web on Vercel's free tier. Report the expected monthly cost in the Phase 1 plan.

**Device abstraction (ADR required).** Everything downstream of the gateway is device-agnostic. Each device type has:
- an **adapter** that authenticates the device (per-device token), validates and normalises its payload into canonical rows, and acknowledges batches idempotently;
- a **capability profile** (which of: live PIDs, Mode 03, Mode 07, Mode 0A, Mode 06, freeze frame, battery voltage, high-rate cranking voltage, GNSS, accelerometer, VIN, remote commands). Indicators and features that need a missing capability are switched off for that vehicle and the UI says so, rather than showing a false "all good".
- v1 builds exactly one adapter: `freematics_one_plus`. The interface must make later adapters (a cheap Bluetooth dongle via a future mobile app built by a mobile developer, a Teltonika tracker, or the teammate's custom cellular device) drop-in additions.

**Device (Freematics ONE+ with LTE module).** ESP32-based, Arduino-programmable, open-source firmware (BSD licence), built-in GNSS and motion sensor, microSD/flash buffering, cellular via its own SIM.
- **Stage A (bring-up):** run the stock open-source telelogger firmware on the test car, pointed at a temporary receiver, to confirm the device reads this car and connects on the chosen SIM.
- **Stage B (AutoCare firmware):** fork it into `firmware/freematics/` and implement: the polling strategy below (including Modes 03/07/0A/06 and VIN), trip detection, store-and-forward buffering, batch upload to the gateway with a device token, an immediate small "critical" packet when on-device `Stop safely` conditions are met, remote commands (Quick Health Scan, change poll rates), and OTA firmware updates.
- **Transport:** HTTPS POST of gzip JSON is preferred. If TLS through the modem proves unreliable or too slow on the ESP32, fall back to HTTP with an HMAC-signed payload (per-device secret, timestamp, nonce) and document it in an ADR.
- **Data budget:** measure upload per driving hour and per month; tune poll rates and batching to keep a cheap Nigerian data plan sufficient. Report the numbers.
- **Power:** sleep when the engine is off; wake on voltage change/motion; must not drain the car battery. Measure parked current draw.
- **Compatibility:** confirm on the actual test car before writing Stage B; record make/model/protocol results in `firmware/COMPATIBILITY.md`.
- Only one device per car. Never plan a second dongle in the same port.

**Polling strategy (adapter throughput is limited, so be deliberate):**
- On connect / trip start: VIN, supported-PID bitmaps, protocol, readiness monitors (0x01), distance and warm-ups since clear (0x31, 0x30), Mode 03 / 07 / 0A, Mode 06, battery voltage.
- Fast group (target 1 Hz, configurable): RPM, speed, engine load, throttle.
- Slow group (every 5 to 10 s): coolant temp, intake air temp, STFT/LTFT, MAF, MAP, O2 sensors, control-module voltage, fuel level.
- Engine start window: high-rate voltage sampling to capture the cranking dip (best effort, document the achievable rate).
- Trip end: Mode 03 / 07, Mode 06.
- Only poll PIDs the vehicle reports as supported.

**Ingestion contract:** batches keyed by `(device_id, trip_id, seq)`, idempotent on retry; per-device auth; clock-skew tolerant (device time, GNSS time when available, server receive time); critical packets processed immediately.

## 8. Data strategy

**Production data model (initial, refine in the Phase 1 plan):**
- `users`, `consents` (purpose, version, granted_at, revoked_at)
- `vehicles` (VIN, make, model, year, engine, displacement, fuel type, protocol, supported PIDs, odometer_at_install), `vehicle_access` (user, vehicle, role)
- `devices` (id, type `freematics_one_plus`, firmware version, token/secret hash, capability profile JSONB, SIM ICCID, data usage, last_seen), `device_assignments` (device ↔ vehicle over time), `device_commands` (queued remote commands and results)
- `trips` (start/end, distance, durations, idle share)
- `readings` in **narrow format** (vehicle_id, trip_id, ts, signal, value) with a BRIN index on `ts`, plus per-minute and per-trip summary tables
- `dtc_events` (code, status stored/pending/permanent, first_seen, last_seen, freeze_frame JSONB), `mode06_results` (mid, tid, value, min, max, unit)
- `trip_indicators`, `baselines` (per vehicle, per indicator), `anomalies`
- `service_items`, `service_rules`, `service_records`
- `alerts` (severity, state, rule id), `insights` (LLM output, prompt version, input hash)
- `repair_feedback` (labels for Layer 5)
- `push_subscriptions`, `dtc_definitions`, `jobs`

**Development datasets** (each gets a data card in `ml/data_cards/`: source, licence, signals, sampling rate, use, limitations). Raw files live in `ml/datasets/raw/`, which is gitignored.

| Dataset | Use in AutoCare |
|---|---|
| Vehicle Energy Dataset (VED), University of Michigan (github.com/gsoh/VED) | Real OBD-II + GPS from 383 cars over a year. Learn "normal" indicator distributions, usage patterns, distance logic. Use petrol vehicles only. |
| Barreto OBD-II datasets (github.com/cephasax/OBDdatasets) | Real ELM327 logs from 14 cars (2003 to 2016) and 1 car with 19 drivers. Use to make the simulator realistic (rates, noise, dropouts). Check for a trouble-codes column. |
| KIT OBD-II dataset (publikationen.bibliothek.kit.edu/1000085073) | Supplementary real signals for simulator calibration. |
| Synthetic Kaggle sets (incl. EV_Predictive_Maintenance_Dataset_15min) | **Pipeline plumbing tests only.** Never used to judge model quality. EV features are out of scope. |
| SCANIA Component X (snd.se/en/catalogue/dataset/2024-34) | **v2 only**, for prototyping survival analysis. |

Rules for all ML work: split by vehicle or by time, never randomly across rows of the same time series; always compare against a trivial baseline; report lead time and false-alarm rate, not accuracy alone.

**OBD simulator (extend the existing one). It carries the whole build until the device arrives, and it powers the public demo.**
- Vehicle profiles: at least Toyota Corolla 1.8L (2009 to 2013), Toyota Camry 2.4L (2007 to 2011), Lexus RX350 3.5L V6, Honda Accord 2.4L, Toyota Sienna 3.5L. Each with supported-PID set and plausible operating ranges.
- Drive cycles: Lagos heavy traffic (long idle, stop-go), urban mixed, expressway cruise, short trips (engine never fully warm).
- Physically consistent relationships (speed, RPM and gear; MAF vs RPM and load; coolant warm-up vs ambient; voltage vs engine state), sensor noise and cellular outages, calibrated on real datasets.
- **Fault injection library** with gradual degradation and ground-truth timelines: weak battery, failing alternator, stuck-open thermostat, low coolant / overheating, intake vacuum leak, dirty MAF, ageing upstream O2 sensor, developing ignition coil misfire (one cylinder), catalyst degradation. Each fault records when its DTC would set (pending, then confirmed).
- Output modes: (a) **emulate the Freematics firmware**: identical batch and critical-packet payloads, device token, outages and late delivery; (b) a virtual ELM327 serial endpoint so `python-OBD`-based tests exercise real protocol parsing (evaluate the open-source ELM327-emulator package before writing one); (c) **demo replay**: pre-generated weeks of data for the demo cars, replayed by a scheduled job.
- Capability profiles: switch capabilities off (e.g. no Mode 06) to test graceful degradation.

## 9. Non-functional requirements

- **Reliability:** at least 99% of buffered readings eventually delivered after coverage gaps; no duplicate rows on retried batches.
- **Latency:** trip processed and insights updated within 5 minutes of upload; `Stop safely` alerts pushed within 30 seconds of the critical packet arriving.
- **Cost:** monthly hosting, LLM spend and device data usage tracked and shown on an admin page. Fewest possible paid services.
- **Low bandwidth UX:** web app usable on slow 3G; small bundles; no heavy assets; PWA caching for the last-seen state.
- **Units and locale:** km, °C, local time (Africa/Lagos), English. Naira cost estimates are out of v1.
- **Observability:** structured logs, request IDs, worker job metrics, device dashboard (last seen, batches/hour, failures, data used).
- **Security:** JWT auth for users, per-device credentials for devices, per-vehicle authorisation on every endpoint, secrets in environment variables, rate limiting on ingestion, firmware OTA images signed or checksummed.

## 10. Privacy and compliance (Nigeria Data Protection Act 2023)

- Once sensor data is tied to an account, a VIN or a location, it is personal data. Design for that from Phase 1.
- Consent-first onboarding with separate, revocable consents: (1) vehicle data processing for the service, (2) location data (GNSS is on the device; allow turning location off, features degrade gracefully), (3) use of pseudonymised data to improve models.
- Pseudonymise vehicle and user IDs in any dataset used for model training; strip or coarsen GPS.
- Data export and account deletion in v1.
- Shared access is explicit and auditable.
- Demo mode uses only simulated data.
- Document the cross-border transfer basis (backend hosted outside Nigeria) in `docs/compliance/`.

## 11. Success metrics for v1

- Simulator + real-data evaluation meets the Layer 3 targets in Section 5.
- Device field test on at least one real car for 4 weeks:
  - At least 90% of trips recorded end to end; full recovery after coverage gaps.
  - Monthly data usage per car fits a cheap data plan (report the number).
  - No measurable battery drain problem when parked.
- At least one genuine early catch (pending code or drifting indicator confirmed by a mechanic) documented as a case study, if one occurs; otherwise a documented injected-fault walkthrough.
- Public demo link works on desktop and phone browsers without login friction.
- Expected monthly running cost for the MVP documented and kept low.

## 12. Out of scope for v1 (do not build or stub)

- Mobile apps (iOS or Android) and any Bluetooth dongle support; a mobile developer may add a Bluetooth path later through the device-adapter interface
- Web Bluetooth
- A second device per car
- EVs and hybrids
- Smartcar or any connected-car OEM API (v2, kept separate so its data doesn't mix with v1 evaluation)
- Time-to-failure / survival models (v2)
- TimescaleDB, Redis, Celery (v1 uses plain Postgres and a Postgres-backed queue; revisit when scale demands)
- Teltonika or other commercial tracker adapters (interface ready; build later)
- Manufacturer-specific PIDs (Mode 22 / UDS), so no TPMS, oil life or brake-wear sensor reads
- Clearing codes (Mode 04) or any vehicle control (Mode 08)
- Repair cost estimates, mechanic marketplace, payments, WhatsApp/SMS alerts
- Diesel-specific logic, motorcycles, heavy trucks

## 13. Build phases (each starts with a plan and ends with green test gates)

Phases 0 to 7 need **no hardware**. Buy the device only when Phase 8 is near and a test car is available.

| Phase | Scope | Exit gate |
|---|---|---|
| **0. Audit** | Inspect repo; map existing work to this PRD; list conflicts; ADRs for: cellular-only + web-only v1, device abstraction, plain Postgres, Postgres job queue | Wisdom approves Phase 1 plan |
| **1. Backend foundation** | Keep health endpoints, Docker, Railway; add async SQLAlchemy + Alembic, core schema (Section 8), auth, users, vehicles, vehicle_access, consents, job queue; expected monthly cost | Migrations run on Railway; auth and vehicle CRUD tested in pytest + Restfox |
| **2. Simulator + device gateway** | Extend simulator (profiles, drive cycles, fault library, capability profiles, Freematics-payload emulation, outages); gateway with `freematics_one_plus` adapter; device tokens; device claim flow; trip assembly; worker pipeline skeleton | Simulated week for 5 cars ingested with zero duplicates; replay idempotent; late/out-of-order batches handled |
| **3. Layer 1 maintenance** | Distance integration, service rules (seed config), forecasts, service records, wear multipliers | Forecasts match hand-calculated cases; edge cases (gaps, odometer mismatch) tested |
| **4. Layer 2 DTC** | Mode 03/07/0A/02 handling, `dtc_definitions` seed, LLM explanation service with RAG, caching, prompt versioning | Explanations for top 50 common codes generated and reviewed by Wisdom; schema validation 100% |
| **5. Layer 3 health + anomaly** | Notebooks on VED/Barreto/simulator → indicators → baselines → scoring → evaluation report; promote to backend; Mode 06 parsing (CAN) | Evaluation report meets targets or Wisdom accepts documented gaps; backend reproduces notebook numbers |
| **6. Layer 4 narrative + alerts** | Severity rules, alert lifecycle with hysteresis, critical-packet fast path, health score, weekly report, web push + email | End to end: injected fault in simulator → correct tier alert → push notification → readable explanation |
| **7. Web app + demo** | Next.js PWA: onboarding, device claim, vehicle overview, live status, maintenance, insights, alerts, fault codes, Quick Health Scan button, sharing, fleet view, repair feedback; public demo mode with replayed simulator data | Usable on phone browsers on a slow network; demo link works logged-out; Wisdom walkthrough sign-off |
| **8. Device bring-up + firmware** | Stage A stock firmware on the test car; Stage B AutoCare firmware (polling, Modes 03/07/0A/06, VIN, buffering, upload, critical packet, remote commands, OTA, sleep) | 2-hour real drive with no data loss; 20-minute no-signal test recovers fully; data per hour and parked current draw measured |
| **9. Field test** | 4 weeks on at least one real car; monitoring; tuning; case study write-up | Section 11 metrics reported |

## 14. Repository layout (proposed; adapt to what exists)

```
autocare/
  backend/
    app/
      api/            # routers: health, auth, vehicles, devices, ingestion, insights, alerts, service, demo
      core/           # config, security, logging
      db/             # session, base, alembic
      models/         # SQLAlchemy models
      schemas/        # Pydantic models
      gateway/
        adapters/     # freematics_one_plus.py (+ future adapters)
      services/
        rules/  dtc/  health/  anomaly/  narrative/  alerts/  notify/
      llm/
        prompts/      # versioned prompt files
      jobs/           # queue interface + worker
      config/         # service_rules.yaml, thresholds.yaml (SEED - validate)
    tests/
  simulator/
    profiles/  cycles/  faults/  outputs/  demo/
  firmware/
    freematics/       # AutoCare firmware fork (Phase 8)
    COMPATIBILITY.md
  ml/
    notebooks/  data_cards/  datasets/ (gitignored)  reports/
  web/                # Next.js PWA
  docs/
    PRD.md  decisions/  compliance/
  CLAUDE.md           # short working rules pointing to docs/PRD.md
```

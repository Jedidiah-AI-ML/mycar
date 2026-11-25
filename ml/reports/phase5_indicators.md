# Phase 5 step 1: health indicators and normal ranges

Notebook: `ml/notebooks/03_health_indicators.ipynb`, built by `_build_03_health_indicators.py`,
run 2026-11-25. The indicator code is `ml/health/indicators.py` (14 unit tests in
`ml/tests/test_indicators.py`). The numbers are in `phase5_indicators.yaml`; all are
`SEED - validate`.

**Data:**
- **simulator:** 10 clean cars (5 profiles × 2 seeds), 30 days each, 731 trips, 8,703 km; plus one car per fault, 45 days;
- **VED:** 4,502 trips of 11+ min from 219 petrol cars;
- **KIT:** 80 trips of one Seat Leon.

## Gate: 19 of 23 checks; the three gaps are accepted by Wisdom (2026-11-25)

Wisdom accepted all three gaps as proposed below. He also accepted adding real-level per-trip
noise to the simulated indicators in step 2.

| Check | Result | Detail |
|---|---|---|
| Coverage ≥ 50 %, 15 of 17 indicators | PASS | 53 % to 100 % |
| Coverage `trim_cruise_b1` / `_b2` | **FAIL** | 42 % of trips (68–70 % of driving days) |
| Sim vs VED: idle trim, cruise trim, idle RPM spread | PASS | Δ −0.78 pp, Δ −0.68 pp, × 0.94 |
| Sim vs VED: `maf_idle_norm` | **FAIL** | sim 2.55 vs VED 1.93 (× 1.32; tolerance 0.7–1.3) |
| VED warm-idle trim within ±10 % on ≥ 90 % | **FAIL** | 86.0 % of 2,084 trips |
| VED idle airflow 1–5 g/s/L/krpm on ≥ 95 % | PASS | 100 % |

### Why each failed (diagnosed, not code bugs)

1. **Cruise trims need steady 50–110 km/h for 30 s, and Lagos traffic rarely gives that.**
   - **Lowering the band doesn't solve it:** from 40 km/h, coverage only reaches 49 % (30 km/h: 54 %). Below 40 km/h the "cruise" reading drifts towards idle conditions, which defeats its purpose. Its purpose is telling a vacuum leak (lean at idle only) from a fuel-delivery fault (lean everywhere).
   - **It's still useful as it is:** a cruise value exists on 68–70 % of driving days. The **idle** trim (53 % of trips, 79 % of days) is the main vacuum-leak signal.
   - **Proposed:** accept the 42 % as a documented limit of Lagos driving.
2. **VED idle trims:** 18 of 188 real cars (10 %) run *consistently* beyond ±10 %, with medians from −17.6 % to +25.8 %.
   - **Without them,** 92.5 % of trips are within ±10 %, which passes.
   - **Warm-up isn't the cause:** restricting to longer trips (15, 20 or 30 min) doesn't change it (86–88 %).
   - **So it's a property of those cars:** probably genuinely lean or rich engines, or sensor offsets. A real fleet contains unhealthy cars, and per-car baselines plus robust (median) priors are built for exactly this.
   - **Proposed:** accept; the priors already use medians, so these cars don't skew them.
3. **Idle airflow level:** the simulator idles with about 32 % more air per litre per 1,000 rpm than VED's (mostly newer, US) cars. Each car's own baseline absorbs the level, and real cars' priors come from VED, not the simulator. **Proposed:** accept and note it. An alternative is to recalibrate the simulator's idle airflow, but that touches the Phase 2 calibration.

## The finding that matters most for step 2: the simulator is far too clean

Within-car (trip-to-trip) spread of a healthy car, robust (MAD × 1.4826):

| Indicator | Simulator | VED (real) | Real ÷ sim |
|---|---|---|---|
| `trim_idle_b1` | 0.30 % | 2.32 % | ≈ 8× |
| `trim_cruise_b1` | 0.45 % | 1.30 % | ≈ 3× |
| `maf_idle_norm` | 0.009 | 0.108 | ≈ 12× |
| `idle_rpm_std` | 0.53 rpm | 6.3 rpm | ≈ 12× |
| `maf_vs_map` | 1.4 % of median | 10.2 % (KIT) | ≈ 7× |

A detector tuned on the simulator alone would look far better than it will be on a real
car. **Proposed for step 2:**
- **Simulated faults:** before tuning and reporting, add per-trip noise to each indicator so its within-car spread matches the real data (VED or KIT; the simulator's own figure where no real data exists).
- **Real-trace injection:** keep the VED experiments (vacuum leak and dirty MAF ramps on real cars' traces), so at least two fault types are judged against real noise.

## Other findings

- **Between-car spread is large for fuel trims** (VED: 2–3.5× the within-car spread) and idle airflow (2.9×). A population threshold would be far too loose for any one car, which is why each car gets its own baseline; the prior is only a starting point.
- **Every fault moves its indicator** (simulator, shift in clean within-car spreads):

  | Fault | Indicator | Shift |
  |---|---|---|
  | Failing alternator | charging voltage | −123 |
  | Vacuum leak | idle trim | +245 |
  | | cruise trim | +46 |
  | Dirty MAF | idle airflow | −75 |
  | | cruise trim | +65 |
  | | MAF vs MAP | −16 |
  | Weak battery | cranking minimum | −65 |
  | | resting voltage | −32 |
  | Low coolant | idle peak coolant | +17 |
  | | overheat seconds | 0 → 536 |
  | Stuck-open thermostat | coolant at 10 min | −16 |
  | Misfire | idle RPM spread | +17 |

  These shifts are inflated by the simulator's low noise, as above. The ageing O2 sensor and catalyst faults are left for Mode 06 (step 3).
- **Vacuum leak barely moves MAF vs MAP** (+2 sd), as physics predicts: leaked air bypasses both the MAF and the throttle, and the ECU corrects with fuel trim. So fuel trim is the right indicator for leaks, and MAF vs MAP for a dirty MAF.
- **Warm-up vs starting temperature (KIT):** −10.7 s per °C of intake air temperature at start (r = −0.31, 57 cold starts). A modest effect. Lagos mornings vary much less than Karlsruhe's, so the detector can use the raw warm-up time. The slope is recorded in case Phase 9 shows it matters.
- **KIT warm-ups** take 736 s (median; p25–p75 562–818 s), close to the simulator's Lagos 858 s.
- **The simulator gives every healthy car identical voltages** (between-car spread 0). Real batteries and alternators differ, so the voltage priors are low confidence, as planned.

## SEED values used (to validate)

| # | Value |
|---|---|
| H1 | Warm idle: coolant ≥ 80 °C, speed ≤ 0.5 km/h, RPM < 1,100, ≥ 30 s |
| H2 | Cold start < 40 °C, warm-up target 80 °C, coolant read 10 min after a cold start, overheat ≥ 105 °C |
| H3 | Resting voltage only after ≥ 1 h parked |
| H7 | Coverage minimum 50 % |
| — | Data with no coolant signal (VED) counts as warm after 10 min |
| — | Cruise: 50–110 km/h, acceleration ≤ 1.5 km/h/s, RPM < 3,500 |
| — | High RPM ≥ 4,000 |
| — | Sim-vs-VED tolerances: trims ± 3 pp; idle RPM spread × 0.5–2; idle airflow × 0.7–1.3 |
| — | KIT engine assumed 1.4 L (airflow levels are relative only) |

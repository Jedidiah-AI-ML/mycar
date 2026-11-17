# ADR-009: Simulated DTCs come from monitors that read the signals, not from fault labels

- **Status:** Accepted (part of the approved Phase 2 plan, step 4)
- **Date:** 2026-11-17

## Context

Step 4 adds nine gradual faults to the simulator and must record when each fault's DTC
goes pending, then confirmed. That timing is the *lead time* AutoCare has to beat: the
whole product is "warn before the check-engine light". There are two ways to decide it:

1. **From the label:** "DTC goes pending when fault severity passes 0.6".
2. **From the signals:** after each trip, a simulated ECU monitor reads that trip's
   signals the way a real one would (e.g. "median fuel trim at warm idle above +25 %"),
   and generic OBD-II two-trip logic turns pass/fail into pending, confirmed and MIL.

## Decision

Option 2. `autocare_sim/dtc.py` runs one monitor per entry in
`simulator/faults/dtc_rules.yaml` (thresholds and enabling conditions are SEED P4). Faults
only change `physics.Health`; codes appear only when the symptoms are strong enough
*on a trip where the monitor could run*.

## Why

- **Realistic timing.** A stuck-open thermostat sets P0128 only after a long cold-start
  trip. A vacuum leak shows sooner on a 1.8 L engine than on a 3.5 L V6, which draws more
  air at idle. A severity cut-off can't produce either effect.
- **No leakage into ML.** If DTC timing were a function of the label, a model could learn
  the label's formula instead of the symptoms. Here the label (fault severity) and the
  DTC are linked only through the simulated signals, as in a real car.
- **Readiness for free.** Monitors that ran since the last clear are exactly the OBD
  readiness status the device will report (step 5).

## Consequences

- Ground truth has two layers: the fault itself (onset, curve) and the DTC timeline the
  ECU produced. The ML phases learn from the fault onset, and the DTC time is the baseline
  to beat.
- Thresholds are SEED and manufacturer-specific. If Wisdom's real scans disagree,
  changing `dtc_rules.yaml` is enough; no code change is needed.
- Simplifications: one-trip codes (e.g. a catalyst-damaging misfire that flashes the MIL)
  aren't modelled; faults only get worse (no repairs yet); if two faults set the same
  Health field, the later one wins.

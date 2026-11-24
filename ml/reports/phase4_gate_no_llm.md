# Phase 4 gate: fault codes explained end to end

Run by `scripts/phase4_gate.py --no-llm` on 2026-11-24 10:06 (7 days, seed 41, LLM off). Local Docker stack only.

**Result: PASSED**: 6 of 6 checks.

## The car

Toyota Corolla 1.8 with a vacuum leak (onset day 1) and a cylinder-2 misfire (day 1). 17 trips, 583 batches over HTTP with Lagos coverage.

| Code | Status | Severity | Stop safely | Source | First seen |
|---|---|---|---|---|---|
| P0171 | stored | service_soon | False | template | 2026-11-21T16:30 |
| P0302 | stored | service_soon | False | template | 2026-11-23T17:15 |
| P0171 | permanent | service_soon | False | template | 2026-11-21T16:30 |
| P0302 | permanent | service_soon | False | template | 2026-11-23T17:15 |

LLM: 0 calls (0 ok, 0 failed), 0 tokens in / 0 out, **$0.0000**, for 22 interpretation runs.

## Checks

| Check | Result | Detail |
|---|---|---|
| the simulator set the injected faults' codes | PASS | simulated ['P0171', 'P0302'] |
| every simulated code is listed as active | PASS | API ['P0171', 'P0302'] vs simulated ['P0171', 'P0302'] |
| every active code is explained, shaped, tiered and grounded | PASS | all clean |
| no dead jobs | PASS | 0 dead |
| dtc.interpret ran for the trips | PASS | 22 runs |
| with no key: templates / reviewed generic, no LLM calls | PASS | 0 calls, sources ['template'] |

## What the owner sees (first code)

**P0171 (stored)**: The engine (bank 1) is running lean: too much air or too little fuel, beyond what the computer can correct.

The engine (bank 1) is running lean: too much air or too little fuel, beyond what the computer can correct.

Likely causes:
1. **vacuum or intake air leak**: The most common cause.
2. **dirty MAF sensor**: A common cause.
3. **weak fuel pump or clogged fuel filter**: A common cause.
4. **clogged injectors**: A common cause.

What to do now: Have it checked within the next few days. Driving on can make it worse or waste fuel.

## Notes

- The car has a VIN and an owner email; neither can reach the model (the context has no field for them, tested in `tests/test_dtc_context.py`).
- Took 88 s.

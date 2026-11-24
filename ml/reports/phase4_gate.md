# Phase 4 gate: fault codes explained end to end

Run by `scripts/phase4_gate.py` on 2026-11-24 10:08 (7 days, seed 41, LLM openrouter/openai/gpt-4o-mini). Local Docker stack only.

**Result: PASSED**: 9 of 9 checks.

## The car

Toyota Corolla 1.8 with a vacuum leak (onset day 1) and a cylinder-2 misfire (day 1). 17 trips, 583 batches over HTTP with Lagos coverage.

| Code | Status | Severity | Stop safely | Source | First seen |
|---|---|---|---|---|---|
| P0171 | stored | service_soon | False | personalised | 2026-11-21T16:30 |
| P0302 | stored | service_soon | False | personalised | 2026-11-23T17:15 |
| P0171 | permanent | service_soon | False | personalised | 2026-11-21T16:30 |
| P0302 | permanent | service_soon | False | personalised | 2026-11-23T17:15 |

LLM: 8 calls (8 ok, 0 failed), 9,941 tokens in / 2,325 out, **$0.0025**, for 21 interpretation runs.

## Checks

| Check | Result | Detail |
|---|---|---|
| the simulator set the injected faults' codes | PASS | simulated ['P0171', 'P0302'] |
| every simulated code is listed as active | PASS | API ['P0171', 'P0302'] vs simulated ['P0171', 'P0302'] |
| every active code is explained, shaped, tiered and grounded | PASS | all clean |
| no dead jobs | PASS | 0 dead |
| dtc.interpret ran for the trips | PASS | 21 runs |
| answers are personalised | PASS | sources ['personalised'] |
| no failed LLM calls (0 schema / grounding failures) | PASS | 8 ok, 0 failed |
| the cache held: fewer calls than interpretation runs x codes | PASS | 8 calls for 21 runs x 2 codes |
| cost well under $0.05 | PASS | $0.0025 |

## What the owner sees (first code)

**P0171 (stored)**: The engine (bank 1) is running lean: too much air or too little fuel, beyond what the computer can correct.

The car's computer has noticed that the engine is running lean, which means there is too much air or not enough fuel getting to the engine. This can lead to performance issues if not checked soon.

Likely causes:
1. **vacuum or intake air leak**: In hot climates, rubber hoses can crack, causing air leaks.
2. **dirty MAF sensor**: Dust can coat the air-flow sensor, making it read less air than there is.
3. **weak fuel pump or clogged fuel filter**: If the fuel pump is weak or the filter is clogged, the engine may not get enough fuel.
4. **clogged injectors**: If the injectors are clogged, they can't deliver the right amount of fuel.

What to do now: Have the car checked within the next few days. Driving on can make the problem worse.

## Notes

- The car has a VIN and an owner email; neither can reach the model (the context has no field for them, tested in `tests/test_dtc_context.py`).
- Took 121 s.

# What AutoCare sends to the LLM provider

Decision Q3 (Phase 4, 2026-11-23): only technical context about a fault code goes to the
model. This note is for the NDPA record (PRD 10) and for anyone changing the LLM code.

## Who receives it

- **OpenRouter** (a router), which passes the request on to **OpenAI**'s `gpt-4o-mini`.
  Both process the request to produce the answer.
- The model and route are set by `LLM_MODEL`. Changing the provider means updating this
  note.

## What is sent, per call

The versioned prompt (`backend/app/llm/prompts/dtc_explain.v2.md`) plus one `DtcContext`
(`backend/app/services/dtc/context.py`):

| Sent | Example |
|---|---|
| The fault code and its status | P0171, stored |
| Our definition: description, system, severity, causes | "running lean", fuel_air, service_soon |
| Make, model, year, engine | Toyota Corolla 2009, 1ZZ-FE 1.8L |
| The freeze frame (engine figures when the code was set) | rpm 780, coolant 91 °C |
| Other active codes on the car, with their descriptions | P0174 |
| 30-day usage in bands | km/day "20 to 50", idle "typical", braking "gentle" |
| 2–3 curated notes (our own text) | "Lean mixture codes on cars in Lagos" |

## What is never sent

- VIN, registration, nickname or any vehicle ID;
- owner name, email or account ID;
- GPS positions, places, trip times or routes;
- device ID or tokens.

The context model has **no field** for any of these and rejects unknown fields. A test
builds the context for a car that has a VIN, an owner email, a nickname and trips, and
checks that none of them appear (`backend/tests/test_dtc_context.py`).

## Retention and logging

- **On our side:** `llm_calls` keeps purpose, model, prompt version, input hash, tokens,
  cost, ok/error and latency. It keeps **no prompt or answer text**. Error text is scrubbed
  of anything that looks like an API key. The answers we show are kept in `insights`, tied
  to the vehicle, and deleted with it.
- **On the provider's side:** governed by OpenRouter's and OpenAI's API data policies. To
  check before launch: that API inputs aren't used for training, and how long they're kept.
- **No personal data** is in the request, so a provider-side retention period doesn't
  expose an owner.

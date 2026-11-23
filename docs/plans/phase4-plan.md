# Phase 4 plan: Layer 2 fault codes (APPROVED 2026-11-23)

**Decisions (Wisdom, 2026-11-23):** all five open questions approved as recommended:
- **Q1:** our own plain-language definitions (~150 generic codes), reviewed by Wisdom
- **Q2:** `openai/gpt-4o-mini` via LiteLLM, with a $2/month cap
- **Q3:** only technical context is sent (no VIN, owner, location or device ID); the transfer basis goes in `docs/compliance/llm-processing.md`
- **Q4:** personalised explanations on, cached by input hash and budget-capped
- **Q5:** curated notes + keyword retrieval, no vector database

**PRD 13, Phase 4:** Mode 03/07/0A/02 handling, `dtc_definitions` seed, an LLM explanation
service with RAG, caching, prompt versioning.
**Exit gate:** explanations for the top 50 common codes generated and **reviewed by Wisdom**;
schema validation 100 %.
**Builds on:** Phase 2 stores every code the car reports (`dtc_events`: stored / pending /
permanent, first/last seen, cleared, freeze frame). Phase 3 gives trip summaries. Nothing
yet says what a code *means*.

**Rule from the PRD (principle 2): rules decide severity; the LLM only explains.** Every
code's severity tier and "stop safely" flag come from the reviewed definitions table, never
from the model.

---

## What Phase 4 builds

```
backend/app/config/dtc/definitions.yaml   ~150 generic codes in our own words (SEED):
        │                                  system, severity tier, stop-safely, causes
        │ synced (like signals / service rules)
migration 0005: dtc_definitions, insights, llm_calls
        │
app/llm/client.py         LiteLLM wrapper: timeout, retry, cost per call, monthly budget cap,
        │                 every call logged (model, prompt version, input hash, tokens, $)
app/llm/prompts/          versioned prompt files (dtc_explain.v1.md)
app/services/dtc/context.py     what the model may use ("retrieval"): definition, car, freeze
        │                        frame, other active codes, recent trips, curated notes
app/services/dtc/explain.py     JSON out → Pydantic + grounding check → cache by input hash
        │                        → template fallback if anything fails
scripts/pregenerate_dtc.py      offline: top-50 generic explanations → reviewed YAML
        │                        + docs/review/dtc_explanations_v1.md for Wisdom
pipeline: trip.assemble → trip.summarise → dtc.interpret   (only when codes changed)
API: GET /vehicles/{id}/codes, GET /vehicles/{id}/codes/{code}  (viewer+)
     GET /admin/llm/usage                                           (admin)
```

Not in Phase 4 (later phases, per PRD 13):
- alerts, push and the health summary narrative (Phase 6; it reuses this LLM layer)
- health indicators and anomalies (Phase 5; the context has a slot for them, empty until then)
- the UI and the Quick Health Scan button (Phase 7)
- receipt-photo parsing (a stretch goal)

## Techniques introduced (plain English)

- **Grounded generation ("RAG").** The model isn't trusted to *know* what a code means. We
  retrieve the facts first (the reviewed definition, the car, the freeze frame, related codes,
  short curated notes) and put only those in the prompt, with an instruction to use nothing
  else. RAG = Retrieval-Augmented Generation: look up, then write.
- **Structured output + validation.** The model must answer in a fixed JSON shape (summary,
  likely causes ranked, what to do, questions for the mechanic). Pydantic checks it. A
  **grounding check** rejects any answer that mentions a code not in its context, or that
  claims a different severity. A failed answer is retried once, then replaced by a
  plain template built from the definition, so the user always gets something correct.
- **Content-addressed cache.** The exact inputs (prompt version + context) are hashed
  (SHA-256). The same inputs never cost a second call: the stored answer is reused. Change
  the prompt version and everything regenerates on demand.
- **Keyword retrieval (BM25-lite).** The curated notes are a few dozen short markdown files.
  Picking the relevant ones by code, system and keyword overlap is enough at this size; no
  vector database or embeddings bill. (pgvector on Neon is the upgrade path if the corpus
  grows.)
- **Budget cap.** Every call's cost is recorded. Past the monthly cap, live generation stops
  and the reviewed generic explanation (or the template) is served instead.

## Steps (dependency order, each with its gate)

### Step 1: Definitions table (licence first)
| File | Purpose |
|---|---|
| `docs/compliance/dtc-definitions-source.md` | Which sources were considered and their licences. **SAE J2012's wording is copyrighted**, and python-OBD's code list is GPL-2.0, so neither can be copied into this repo as-is. Recommended: our own plain-language descriptions of generic codes (facts about what a code covers aren't copyrightable; the wording is ours) |
| `backend/app/config/dtc/definitions.yaml` | ~150 generic codes (P0, P2, U0, a few C0/B0), each with: our own description, system (fuel/air, ignition, emissions, cooling, charging, network, …), default severity tier (Info / Watch / Service soon / Stop safely), stop-safely flag, common causes, and whether it can be pending. Must include all 13 codes the simulator's faults produce. **All SEED** |
| `backend/app/config/dtc/top50.yaml` | The 50 codes for the PRD gate: the commonest generic codes on petrol cars (misfire P0300–P0306, lean/rich P0171/P0172/P0174/P0175, catalyst P0420/P0430, EVAP P0440–P0456, O2 sensors and heaters, MAF/MAP, thermostat P0128, cam/crank, VVT P0011/P0014, idle control, voltage P0562/P0563, overheat P0217, transmission P0700/P0715/P0740, network U0100). **SEED: Wisdom confirms the list** |
| migration `0005_dtc_llm` | `dtc_definitions` (synced from config), `insights`, `llm_calls` (tables below) |

**Gate:** pytest: config validates (code format, known system and tier, causes present); every simulator code has a definition; every top-50 code has a definition; sync is idempotent; migration round trip and `alembic check` are clean.

✅ **Done 2026-11-23:** 21 new tests (369 backend tests in total, all pass).
- **`definitions.yaml`: 184 generic codes in our own words** (172 P, 7 U, 4 C, 1 B), including all 14 codes the simulator's faults can set.
  - By tier: 4 stop safely (P0217 overheat, P0218 transmission overheat, P0524 low oil pressure, P2111 throttle stuck open), 105 service soon, 60 watch, 15 info (mostly fuel-cap/EVAP).
  - 18 codes carry an `escalation` note for Phase 6's rules (e.g. a misfire → stop safely if the light flashes).
- **Validation:** strict, and found a real bug at once: an unquoted colon in P0700's causes had turned it into a mapping.
- **The flag must match the tier:** `stop_safely` must equal `severity == stop_safely`, both in the config and as a DB CHECK.
- **`top50.yaml`:** 50 defined codes for the PRD gate.
- **Migration 0005** (round trip and `alembic check` clean): `dtc_definitions`, `insights` (its unique key `(vehicle, kind, subject, input_hash)` with NULLS NOT DISTINCT is the cache), `llm_calls` (cost history survives a deleted vehicle).
- **`docs/compliance/dtc-definitions-source.md`:** why SAE J2012 and python-OBD (GPL-2.0) weren't copied, and what we wrote instead.

### Step 2: LLM client, prompts, schemas
| File | Purpose |
|---|---|
| `backend/app/llm/client.py` | One `complete_json()` call through **LiteLLM** (model from `LLM_MODEL`, currently `openai/gpt-4o-mini`): timeout, one retry, token and cost accounting (LiteLLM's price table), a monthly budget cap (`LLM_MONTHLY_BUDGET_USD`, SEED $2), and a row in `llm_calls` for every attempt. No key → disabled cleanly (templates only) |
| `backend/app/llm/prompts/dtc_explain.v1.md` | The versioned prompt: role, reading level (~12-year-old, PRD 5), rules (use only the context; never change severity; pending codes get their own wording), the JSON shape |
| `backend/app/llm/schemas.py` | `DtcExplanation` (summary, likely causes ranked with why, what to do now, what to ask the mechanic, how sure: high/medium/low) + the grounding validator |
| `backend/app/services/dtc/templates.py` | The fallback: a correct, plain explanation built from the definition alone |

**Gate:** pytest with a **fake LLM** (no network in tests): a valid answer is stored; bad JSON → retried, then template; an invented code → rejected → template; a changed severity → rejected; over budget → template with no call; no key → template; every attempt logged with tokens and cost.

### Step 3: Context builder (retrieval)
| File | Purpose |
|---|---|
| `backend/app/knowledge/*.md` | Short curated notes per system / code family (e.g. "lean codes on Lagos cars: vacuum leaks, dirty MAF, fuel pressure, adulterated fuel"). Written by me, **reviewed by Wisdom (SEED)**, with no vehicle data |
| `backend/app/services/dtc/context.py` | Builds the model's whole world for one explanation: the definition, the car's make/model/year/engine (**no VIN, no owner, no location**), the freeze frame, other active codes on the car, the last trips' summaries (Phase 3), a slot for Phase 5 indicators, and the top 2–3 notes by code/system/keyword match. Deterministic and serialisable, so it can be hashed |

**Gate:** pytest: the right notes are retrieved for sample codes; no personal data can reach the context (no VIN, user, email or position fields exist in it); the same inputs → the same context → the same hash.

### Step 4: Offline pre-generation and Wisdom's review (the PRD gate)
| File | Purpose |
|---|---|
| `scripts/pregenerate_dtc.py` | For each top-50 code: a **generic** explanation (a typical petrol car, no freeze frame, stored and pending variants) through the real model. Validated and grounding-checked; anything failing is reported, never silently stored |
| `backend/app/config/dtc/explanations.v1.yaml` | The generated explanations, each with `reviewed: false` until Wisdom marks it `true`. Only reviewed ones are served as "generic" explanations |
| `docs/review/dtc_explanations_v1.md` | A readable sheet of all 50 (×2 for pending) for Wisdom to review and correct |

**Cost:** about 100 calls at roughly $0.0005 each = **about $0.05 once** (estimate from gpt-4o-mini's published price, to check on the provider's page).

**Gate (PRD):** 100 % of the generated explanations are schema-valid and grounding-clean, and **Wisdom reviews the sheet**: corrections are applied, and each code is marked reviewed.

### Step 5: Interpretation stage + API
| File | Purpose |
|---|---|
| `backend/app/jobs/handlers/dtc_interpret.py` | Pipeline stage after `trip.summarise`, only for trips whose batches brought code changes. For each active code: if a **personalised** explanation for this exact context is cached, reuse it; else (within budget, Q4) generate one; else serve the reviewed generic or the template. Stored in `insights` |
| `backend/app/api/codes.py` | `GET /vehicles/{id}/codes` (viewer+): active codes grouped stored / pending / permanent, each with its definition, severity tier, stop-safely flag, first/last seen, freeze frame, and its explanation (the source is shown: personalised / reviewed generic / template). Recently cleared codes are listed too. `GET /vehicles/{id}/codes/{code}`: one code in full, with its history. `GET /admin/llm/usage`: calls, tokens and $ this month vs the cap |
| `backend/restfox/phase4_requests.py` | The Phase 4 request list |

**Gate:** pytest:
- role matrix
- pending codes use the pending message (PRD 5)
- a second identical request makes no LLM call (cache)
- severity always equals the definition's
- an unknown code (not in the table) still gets a template answer that says so
- the Restfox list passes locally

### Step 6: End to end + live
`scripts/phase4_gate.py`: a simulated car develops a vacuum leak and a misfire (Phase 2 faults). Their codes (P0171, P0302) are ingested over HTTP, interpreted in the pipeline, and `GET /codes` shows a validated, correctly tiered explanation for each, with a fake LLM locally (no cost). Then deploy, and run the Restfox list on Railway with the real model (a few cents).

**Gate:** the report shows the injected faults' codes explained, severities equal to the definitions, 0 schema failures; the list passes live.

## Phase 4 schema (migration 0005)

| Table | Key columns | Notes |
|---|---|---|
| `dtc_definitions` | `code` PK, `description`, `system`, `severity` (info / watch / service_soon / stop_safely), `stop_safely` bool, `causes` jsonb, `source`, `version` | Synced from config |
| `insights` | `id`, `vehicle_id`, `kind` (`dtc_explanation`; Phase 6 adds more), `subject` (e.g. the code), `content` jsonb, `source` (personalised / generic / template), `prompt_version`, `input_hash`, `created_at` | Unique `(vehicle_id, kind, subject, input_hash)`: the cache |
| `llm_calls` | `id`, `ts`, `purpose`, `model`, `prompt_version`, `input_hash`, `tokens_in`, `tokens_out`, `cost_usd`, `ok`, `error`, `latency_ms`, `vehicle_id` (nullable) | Every attempt; feeds the cost cap and the admin page |

## Privacy (NDPA, PRD 10)

What leaves our servers for the LLM provider: the code, our definition, the car's make, model, year and engine, freeze-frame values, other active codes, recent trip figures and notes. **Never** the VIN, the owner, an email, positions or the device ID. A test enforces this (step 3). `docs/compliance/llm-processing.md` records the provider, what is sent, the cross-border transfer basis and retention, as PRD 10 asks.

## Expected monthly cost

| Item | Estimate |
|---|---|
| Pre-generating the top 50 (once) | ~$0.05 |
| Live personalised explanations | a few new codes per car per month, ~$0.001 each → **well under $0.10 per car-month** |
| Hard cap | `LLM_MONTHLY_BUDGET_USD` = **$2** (SEED); past it, users get the reviewed generic explanations |

**New monthly bill:** the LLM provider's pay-as-you-go account (OpenAI, per `.env`); expected cents. No new hosted service; no vector database.

## Open questions for Wisdom

1. **Definitions source:** write our own plain-language definitions for ~150 generic codes, with you reviewing them *(recommended: no licence risk, and the wording is ours)*, or try to import a larger open list once its licence is confirmed?
2. **Model:** keep `openai/gpt-4o-mini` via LiteLLM, as configured *(recommended: cheap, good at structured JSON; switching later is one setting)*, with a **$2/month** cap?
3. **Privacy:** send only the technical context above, with no VIN, owner, location or device ID *(recommended)*. I'll write `docs/compliance/llm-processing.md` for the transfer basis.
4. **Personalised explanations:** on (cached by input hash, budget-capped) *(recommended)*, or reviewed generic explanations only until Phase 6?
5. **Retrieval:** curated notes + keyword matching, with no vector database *(recommended at this size)*, or pgvector embeddings now?

## SEED values to validate

| # | Value | Where | Why unsure |
|---|---|---|---|
| D1 | ~150 code definitions: descriptions, systems, causes | `config/dtc/definitions.yaml` | Our own wording from general knowledge; Wisdom's diagnostics experience is the check |
| D2 | Severity tier and stop-safely flag per code | same | Product + safety judgement (PRD 5: stop-safely is deterministic) |
| D3 | The top-50 list | `config/dtc/top50.yaml` | "Common" is from general experience, not Nigerian repair-shop data |
| D4 | Curated notes (e.g. adulterated fuel, heat, dust) | `app/knowledge/` | Local knowledge Wisdom has and I don't |
| D5 | Monthly LLM budget cap: $2 | settings | A cost choice |
| D6 | Prompt wording and reading level | `llm/prompts/dtc_explain.v1.md` | Reviewed through the 50 generated explanations |

## New ADRs in this phase

- **ADR-013: LLM explanations: grounded, validated, cached, capped.** How the model is kept to the facts, what happens when it fails, how cost stays bounded, what data may leave.

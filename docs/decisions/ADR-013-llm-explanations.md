# ADR-013: LLM explanations are grounded, validated, cached and capped

- **Status:** Accepted (Phase 4, decisions Q2–Q5, 2026-11-23)
- **Date:** 2026-11-24

## Context

Layer 2 explains fault codes to owners who aren't mechanics. A language model writes
much better plain-language explanations than a template, but left to itself it can:
- invent facts or other fault codes;
- make a problem sound more or less urgent than it is;
- cost money on every call;
- be sent personal data.

PRD 5 says the severity tier comes from rules, never from the model.

## Decision

1. **Grounded.** The model sees one `DtcContext` and is told to use nothing else (prompt
   `dtc_explain.v2`). The context holds:
   - our reviewed definition;
   - make, model, year and engine;
   - the freeze frame;
   - the other active codes;
   - 30-day usage **bands**;
   - 2–3 curated notes picked by keyword score (no vector database, decision Q5).
2. **Validated.** The answer must parse into `DtcExplanation`. It must then pass the
   grounding check:
   - its severity equals the definition's;
   - it mentions no code outside the car's active codes.

   A failed answer is retried once, then replaced by a template.
3. **Fallbacks are always correct.** If there's no key, the budget is spent, the provider
   is down or the answer is rejected, the owner gets one of two fallbacks:
   - the **reviewed generic** explanation, pre-generated for the top 50 codes and approved
     by Wisdom;
   - otherwise the **template**, built from the definition alone.
4. **Cached.** The SHA-256 of the prompt version plus the canonical context JSON is the
   cache key in `insights`. The same car with the same facts is never explained twice.
   Usage is banded so the key doesn't change after every drive. Rejected answers are
   cached too; skipped ones are retried later.
5. **Capped and accounted.** Every attempt is a row in `llm_calls` with tokens and cost.
   Generation stops at `LLM_MONTHLY_BUDGET_USD` (SEED $2). Calls happen only in the
   `dtc.interpret` pipeline stage, never in a request.
6. **No personal data.** The context model has **no field** for a VIN, owner, email,
   location or device, and it rejects unknown fields. See
   `docs/compliance/llm-processing.md`.
7. **Provider-agnostic.** All calls go through LiteLLM. The model is one setting,
   `LLM_MODEL`, currently `openrouter/openai/gpt-4o-mini` because the key is an
   OpenRouter key.

## Why

- **Severity comes from rules,** so an urgent code can't be talked down, and a minor one
  can't cause panic. The grounding check enforces this on every answer.
- **The cache and the cap together** bound cost: about $0.0003 per new context, $0.055
  for all the pre-generation, and $2/month worst case.
- **No LLM call in the request path,** so pages never wait on a provider, and a provider
  outage never breaks the app.

## Consequences

- A new prompt version changes every cache key. Each car's codes are re-explained once,
  and the top-50 must be regenerated and re-reviewed.
- Editing a definition or a note makes that code's reviewed explanation stale. It isn't
  served until it's regenerated and reviewed again.
- Owners may see a template until the next trip is interpreted.
- Adding a provider is a settings change. If LiteLLM's price list lacks the route, the
  cost falls back to the base model's per-token price.

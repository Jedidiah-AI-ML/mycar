"""Explain one fault code: ask the LLM within its rules, or fall back (ADR-013).

1. A code we have no definition for -> template ("not in our list yet"). The model never
   explains a code we can't ground.
2. Otherwise up to **two** LLM attempts. An attempt counts only if its answer is valid
   JSON in the `DtcExplanation` shape **and** passes the grounding check (no invented
   codes, severity equal to the definition's).
3. No key, over budget, or two failed attempts -> the template, which is always correct.

Returns the explanation and where it came from: `personalised` (LLM, with this car's
facts), `generic` (LLM, no car: the pre-generated top-50) or `template`.
"""

from dataclasses import dataclass, field

from pydantic import ValidationError

from app.llm import client
from app.llm.prompting import load_prompt
from app.llm.schemas import DtcExplanation, grounding_problems
from app.services.dtc.context import DtcContext
from app.services.dtc.templates import template_explanation

PROMPT_VERSION = "dtc_explain.v1"
PURPOSE = "dtc_explain"
MAX_ATTEMPTS = 2


@dataclass
class ExplainResult:
    explanation: DtcExplanation
    source: str  # personalised / generic / template
    prompt_version: str
    input_hash: str
    attempts: int = 0
    problems: list[str] = field(default_factory=list)  # why LLM answers were rejected
    skipped: str | None = None  # no_key / over_budget / no_definition


async def explain(session, ctx: DtcContext, vehicle_id=None) -> ExplainResult:
    input_hash = ctx.input_hash(PROMPT_VERSION)
    out = ExplainResult(template_explanation(ctx), "template", PROMPT_VERSION, input_hash)
    if ctx.definition is None:
        out.skipped = "no_definition"
        return out
    prompt = load_prompt(PROMPT_VERSION)
    user = prompt.render_user(context_json=ctx.canonical_json(), code=ctx.code, status=ctx.status)
    for _ in range(MAX_ATTEMPTS):
        res = await client.complete_json(
            session, purpose=PURPOSE, prompt_version=PROMPT_VERSION, system=prompt.system,
            user=user, input_hash=input_hash, vehicle_id=vehicle_id,
        )  # fmt: skip
        if res.skipped:
            out.skipped = res.skipped
            return out
        out.attempts += 1
        if not res.ok:
            out.problems.append(res.error or "call failed")
            continue
        try:
            answer = DtcExplanation.model_validate(res.data)
        except ValidationError as e:
            out.problems.append(f"schema: {e.errors()[0]['loc']} {e.errors()[0]['msg']}")
            continue
        if problems := grounding_problems(answer, ctx):
            out.problems += problems
            continue
        out.explanation = answer
        out.source = "personalised" if ctx.vehicle is not None else "generic"
        return out
    return out

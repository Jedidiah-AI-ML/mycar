"""The answer shape the LLM must return, and the grounding check (ADR-013).

Schema validation (Pydantic) catches a malformed answer. The grounding check catches a
well-formed answer that broke the rules:
- it mentions a fault code that wasn't in its context (invented codes);
- its severity differs from the definition's (the LLM never decides urgency, PRD 5).
Either way the answer is rejected: retried once, then replaced by a template.
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.services.dtc.context import CODE_RE, DtcContext


class Cause(BaseModel):
    model_config = ConfigDict(extra="forbid")

    cause: str = Field(min_length=3, max_length=200)
    why: str = Field(min_length=3, max_length=300)


class DtcExplanation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    summary: str = Field(min_length=20, max_length=600)
    severity: Literal["info", "watch", "service_soon", "stop_safely"]
    likely_causes: list[Cause] = Field(min_length=1, max_length=5)
    what_to_do_now: str = Field(min_length=10, max_length=400)
    questions_for_mechanic: list[str] = Field(min_length=1, max_length=4)
    confidence: Literal["high", "medium", "low"]


def grounding_problems(answer: DtcExplanation, ctx: DtcContext) -> list[str]:
    """Why this answer can't be shown, or [] if it can."""
    problems = []
    if ctx.definition is not None and answer.severity != ctx.definition.severity:
        want = ctx.definition.severity
        problems.append(f"severity {answer.severity!r} differs from the definition's {want!r}")
    texts = [answer.summary, answer.what_to_do_now, *answer.questions_for_mechanic]
    texts += [t for c in answer.likely_causes for t in (c.cause, c.why)]
    mentioned = {m for t in texts for m in CODE_RE.findall(t)}
    if invented := sorted(mentioned - ctx.allowed_codes):
        problems.append(f"mentions codes not in its context: {invented}")
    return problems

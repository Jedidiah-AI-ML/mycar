from datetime import datetime
from typing import Literal

from pydantic import BaseModel

from app.llm.schemas import Cause

Source = Literal["personalised", "generic", "template"]
Severity = Literal["info", "watch", "service_soon", "stop_safely"]


class ExplanationOut(BaseModel):
    source: Source  # personalised (LLM, this car) / generic (reviewed) / template
    summary: str
    severity: Severity
    likely_causes: list[Cause]
    what_to_do_now: str
    questions_for_mechanic: list[str]
    confidence: Literal["high", "medium", "low"]
    prompt_version: str | None = None
    generated_at: datetime | None = None  # None: worked out now (no stored one yet)


class CodeOut(BaseModel):
    code: str
    status: Literal["stored", "pending", "permanent"]
    known: bool  # in AutoCare's list of definitions
    description: str | None
    system: str | None
    severity: Severity
    stop_safely: bool
    first_seen: datetime
    last_seen: datetime
    cleared_at: datetime | None = None
    freeze_frame: dict[str, float] | None = None
    explanation: ExplanationOut | None = None  # None for cleared codes


class CodesOut(BaseModel):
    stop_safely: bool  # any active code says stop safely
    stored: list[CodeOut]
    pending: list[CodeOut]
    permanent: list[CodeOut]
    recently_cleared: list[CodeOut]


class CodeDetailOut(BaseModel):
    code: str
    known: bool
    description: str | None
    system: str | None
    severity: Severity
    stop_safely: bool
    causes: list[str]
    escalation: str | None
    active: list[CodeOut]
    history: list[CodeOut]  # every time this car reported it, newest first


class PurposeUsage(BaseModel):
    purpose: str
    calls: int
    cost_usd: float


class LlmUsageOut(BaseModel):
    month: str  # YYYY-MM (UTC)
    enabled: bool
    model: str
    cap_usd: float
    spent_usd: float
    remaining_usd: float
    calls: int
    ok: int
    failed: int
    tokens_in: int
    tokens_out: int
    by_purpose: list[PurposeUsage]
    insights_by_source: dict[str, int]  # explanations stored this month

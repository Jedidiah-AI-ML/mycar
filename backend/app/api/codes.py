"""Fault-code API (Phase 4 step 5). Read-only: explanations are made by the `dtc.interpret`
pipeline stage, never during a request, so these routes never call the LLM.

- `GET /vehicles/{id}/codes` (viewer+): active codes by status (stored / pending /
  permanent), most severe first, each with its definition, severity, stop-safely flag,
  when it was seen, the freeze frame and its explanation (`source` says where it came
  from); plus codes cleared in the last 30 days.
- `GET /vehicles/{id}/codes/{code}` (viewer+): one code in full, with its history.
- `GET /admin/llm/usage` (admin): this month's calls, tokens and $ against the cap.

Severity always comes from our definition, never from the model. A code with no stored
explanation yet gets the reviewed generic one or the template, worked out on the spot.
"""

from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, HTTPException, Path, status
from sqlalchemy import func, select

from app.api.admin import AdminUser
from app.api.deps import AsViewer, SessionDep
from app.core.config import get_settings
from app.llm import client
from app.models import DtcEvent, Insight, LlmCall
from app.schemas.codes import CodeDetailOut, CodeOut, CodesOut, ExplanationOut, LlmUsageOut
from app.services.dtc import definitions as dd
from app.services.dtc.build import generic_context
from app.services.dtc.interpret import KIND, fallback, latest, subject

router = APIRouter(prefix="/vehicles/{vehicle_id}", tags=["codes"])
admin_router = APIRouter(prefix="/admin", tags=["admin"])

CLEARED_WINDOW_DAYS = 30
RANK = {"stop_safely": 0, "service_soon": 1, "watch": 2, "info": 3}
CodePath = Annotated[str, Path(pattern=r"^[PCBU][0-9A-F]{4}$", examples=["P0171"])]


def _explanation(e: DtcEvent, stored: Insight | None) -> ExplanationOut:
    if stored is not None:
        return ExplanationOut(**stored.content, source=stored.source,
                              prompt_version=stored.prompt_version,
                              generated_at=stored.created_at)  # fmt: skip
    answer, source = fallback(generic_context(e.code, e.status.value))
    return ExplanationOut(**answer.model_dump(), source=source)


def _code_out(e: DtcEvent, stored: Insight | None = None, *, explain: bool = True) -> CodeOut:
    d = dd.get(e.code)
    exp = _explanation(e, stored) if explain else None
    return CodeOut(
        code=e.code, status=e.status.value, known=d is not None,
        description=d.desc if d else None, system=d.system if d else None,
        severity=d.severity if d else (exp.severity if exp else "watch"),
        stop_safely=d.stop_safely if d else False,
        first_seen=e.first_seen, last_seen=e.last_seen, cleared_at=e.cleared_at,
        freeze_frame=e.freeze_frame, explanation=exp,
    )  # fmt: skip


def _ordered(items: list[CodeOut]) -> list[CodeOut]:
    return sorted(items, key=lambda c: (RANK[c.severity], c.code))


@router.get("/codes", response_model=CodesOut)
async def list_codes(ctx: AsViewer, session: SessionDep):
    vid = ctx.vehicle.id
    since = datetime.now(UTC) - timedelta(days=CLEARED_WINDOW_DAYS)
    events = (
        await session.scalars(
            select(DtcEvent).where(
                DtcEvent.vehicle_id == vid,
                (DtcEvent.cleared_at.is_(None)) | (DtcEvent.cleared_at >= since),
            )
        )
    ).all()
    active = [e for e in events if e.cleared_at is None]
    stored = await latest(session, vid, [subject(e.code, e.status.value) for e in active])
    by_status: dict[str, list[CodeOut]] = {"stored": [], "pending": [], "permanent": []}
    for e in active:
        by_status[e.status.value].append(_code_out(e, stored.get(subject(e.code, e.status.value))))
    cleared = sorted(
        (_code_out(e, explain=False) for e in events if e.cleared_at is not None),
        key=lambda c: c.cleared_at, reverse=True,
    )  # fmt: skip
    every = [c for group in by_status.values() for c in group]
    return CodesOut(
        stop_safely=any(c.stop_safely for c in every),
        **{k: _ordered(v) for k, v in by_status.items()},
        recently_cleared=cleared[:20],
    )


@router.get("/codes/{code}", response_model=CodeDetailOut)
async def get_code(code: CodePath, ctx: AsViewer, session: SessionDep):
    vid = ctx.vehicle.id
    events = (
        await session.scalars(
            select(DtcEvent)
            .where(DtcEvent.vehicle_id == vid, DtcEvent.code == code)
            .order_by(DtcEvent.last_seen.desc(), DtcEvent.id.desc())
        )
    ).all()
    if not events:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "This car hasn't reported that code")
    active = [e for e in events if e.cleared_at is None]
    stored = await latest(session, vid, [subject(e.code, e.status.value) for e in active])
    d = dd.get(code)
    active_out = [_code_out(e, stored.get(subject(e.code, e.status.value))) for e in active]
    return CodeDetailOut(
        code=code, known=d is not None, description=d.desc if d else None,
        system=d.system if d else None,
        severity=d.severity if d else "watch", stop_safely=d.stop_safely if d else False,
        causes=list(d.causes) if d else [], escalation=d.escalation if d else None,
        active=active_out, history=[_code_out(e, explain=False) for e in events],
    )  # fmt: skip


@admin_router.get("/llm/usage", response_model=LlmUsageOut)
async def llm_usage(admin: AdminUser, session: SessionDep):
    s = get_settings()
    start = datetime.now(UTC).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    calls, ok, tin, tout, cost = (
        await session.execute(
            select(
                func.count(), func.count().filter(LlmCall.ok),
                func.coalesce(func.sum(LlmCall.tokens_in), 0),
                func.coalesce(func.sum(LlmCall.tokens_out), 0),
                func.coalesce(func.sum(LlmCall.cost_usd), 0),
            ).where(LlmCall.ts >= start)
        )
    ).one()  # fmt: skip
    purposes = (
        await session.execute(
            select(LlmCall.purpose, func.count(), func.coalesce(func.sum(LlmCall.cost_usd), 0))
            .where(LlmCall.ts >= start)
            .group_by(LlmCall.purpose)
            .order_by(LlmCall.purpose)
        )
    ).all()
    sources = (
        await session.execute(
            select(Insight.source, func.count())
            .where(Insight.kind == KIND, Insight.created_at >= start)
            .group_by(Insight.source)
        )
    ).all()
    spent = float(cost)
    return LlmUsageOut(
        month=f"{start:%Y-%m}", enabled=client.enabled(), model=s.llm_model,
        cap_usd=s.llm_monthly_budget_usd, spent_usd=round(spent, 6),
        remaining_usd=round(max(0.0, s.llm_monthly_budget_usd - spent), 6),
        calls=calls, ok=ok, failed=calls - ok, tokens_in=int(tin), tokens_out=int(tout),
        by_purpose=[{"purpose": p, "calls": n, "cost_usd": float(c)} for p, n, c in purposes],
        insights_by_source={src: n for src, n in sources},
    )  # fmt: skip

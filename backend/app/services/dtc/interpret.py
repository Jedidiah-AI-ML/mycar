"""Turn a car's active fault codes into explanations, stored in `insights` (step 5).

Runs in the `dtc.interpret` pipeline stage, never in a request: the API only reads.

For each active code (its subject is "P0171:stored"):
1. **Cached:** an insight for this exact context (input hash) exists -> nothing to do. An
   unchanged code on an unchanged car never costs a second call.
2. Otherwise ask the LLM (`explain()`: grounded, validated, at most two attempts).
   - A **personalised** answer is stored under its hash.
   - Two **rejected** answers: the fallback is stored under the hash too, so the same
     failure isn't paid for again on every trip.
   - **Skipped** (no key, over budget, provider down): the fallback is stored with no
     hash (one row per subject, replaced each time), so a later run retries once the
     budget allows. If the car already has a personalised answer for this subject, that
     one stays on show instead.
3. The fallback is the **reviewed generic** explanation if Wisdom has approved one for this
   code and status, else the **template** (always correct, from the definition alone).
"""

from dataclasses import dataclass, field

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert

from app.llm.schemas import DtcExplanation
from app.models import DtcEvent, Insight, Vehicle
from app.services.dtc import pregenerated
from app.services.dtc.build import build_context
from app.services.dtc.context import DtcContext
from app.services.dtc.explain import PROMPT_VERSION, explain
from app.services.dtc.templates import template_explanation

KIND = "dtc_explanation"
RETRY_LATER = ("no_key", "over_budget", "provider_error")


def subject(code: str, status: str) -> str:
    return f"{code}:{status}"


@dataclass
class Outcome:
    cached: list[str] = field(default_factory=list)
    personalised: list[str] = field(default_factory=list)
    generic: list[str] = field(default_factory=list)
    template: list[str] = field(default_factory=list)
    kept: list[str] = field(default_factory=list)  # skipped; an older personalised stays


def fallback(ctx: DtcContext) -> tuple[DtcExplanation, str]:
    if ctx.definition is not None and (g := pregenerated.served(ctx.code, ctx.status)):
        return g, "generic"
    return template_explanation(ctx), "template"


async def _store(session, vehicle_id, subj, answer, source, prompt_version, input_hash):
    stmt = insert(Insight).values(
        vehicle_id=vehicle_id, kind=KIND, subject=subj, content=answer.model_dump(),
        source=source, prompt_version=prompt_version, input_hash=input_hash,
    )  # fmt: skip
    if input_hash is None:  # the retry-later row: replace it
        stmt = stmt.on_conflict_do_update(
            constraint="uq_insights_cache",
            set_={"content": stmt.excluded.content, "source": stmt.excluded.source,
                  "prompt_version": stmt.excluded.prompt_version, "created_at": func.now()},
        )  # fmt: skip
    else:
        stmt = stmt.on_conflict_do_nothing(constraint="uq_insights_cache")
    await session.execute(stmt)


async def _exists(session, vehicle_id, subj, **where) -> bool:
    q = select(Insight.id).where(
        Insight.vehicle_id == vehicle_id, Insight.kind == KIND, Insight.subject == subj
    )
    for k, v in where.items():
        q = q.where(getattr(Insight, k) == v)
    return await session.scalar(q.limit(1)) is not None


async def interpret_event(session, vehicle: Vehicle, event: DtcEvent, out: Outcome) -> None:
    subj = subject(event.code, event.status.value)
    ctx = await build_context(session, vehicle, event)
    h = ctx.input_hash(PROMPT_VERSION)
    if await _exists(session, vehicle.id, subj, input_hash=h):
        out.cached.append(subj)
        return
    res = await explain(session, ctx, vehicle_id=vehicle.id)
    if res.source == "personalised":
        await _store(session, vehicle.id, subj, res.explanation, "personalised", PROMPT_VERSION, h)
        out.personalised.append(subj)
        return
    if res.skipped in RETRY_LATER and await _exists(
        session, vehicle.id, subj, source="personalised"
    ):
        out.kept.append(subj)
        return
    answer, source = fallback(ctx)
    key = None if res.skipped in RETRY_LATER else h  # rejected / unknown code: cache it
    await _store(session, vehicle.id, subj, answer, source, PROMPT_VERSION, key)
    getattr(out, source).append(subj)


async def interpret_vehicle(session, vehicle_id, *, commit_each: bool = False) -> Outcome:
    """`commit_each`: commit after every code, so money spent on LLM calls stays recorded
    (and their answers cached) even if a later code fails and the job is retried."""
    out = Outcome()
    vehicle = await session.get(Vehicle, vehicle_id)
    if vehicle is None or vehicle.deleted_at is not None:
        return out
    events = (
        await session.scalars(
            select(DtcEvent)
            .where(DtcEvent.vehicle_id == vehicle_id, DtcEvent.cleared_at.is_(None))
            .order_by(DtcEvent.code, DtcEvent.status)
        )
    ).all()
    for e in events:
        await interpret_event(session, vehicle, e, out)
        if commit_each:
            await session.commit()
    return out


# ---------------------------------------------------------------- reading (the API)


async def latest(session, vehicle_id, subjects: list[str]) -> dict[str, Insight]:
    """The newest stored explanation per subject."""
    if not subjects:
        return {}
    rows = (
        await session.scalars(
            select(Insight)
            .where(Insight.vehicle_id == vehicle_id, Insight.kind == KIND,
                   Insight.subject.in_(subjects))
            .order_by(Insight.created_at.desc(), Insight.id.desc())
        )
    ).all()  # fmt: skip
    newest: dict[str, Insight] = {}
    for r in rows:
        newest.setdefault(r.subject, r)
    return newest

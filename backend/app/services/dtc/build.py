"""Fill a `DtcContext` from the database: the model's whole world for one code (ADR-013).

What goes in, and nothing else:
- the reviewed definition (severity and causes come from here, never from the model)
- the car's make, model, year and engine (no VIN, owner, location or device: the context
  model has no field for them, and this module never reads them)
- this code's freeze frame (the engine's figures when the code was set)
- the other codes active on the car: the only other codes the answer may mention
- how the car has been used over the last 30 days, in coarse bands (`usage`)
- the 2-3 most relevant curated notes

`generic_context()` is the same without a car: the pre-generated top-50 explanations.
"""

from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select

from app.models import DtcEvent, Trip, Vehicle
from app.services.dtc import definitions as dd
from app.services.dtc.context import (
    DefinitionFacts,
    DtcContext,
    Note,
    OtherCode,
    UsageFacts,
    VehicleFacts,
)
from app.services.dtc.notes import relevant
from app.services.service_rules import wear

USAGE_WINDOW_DAYS = 30


def facts(d: dd.Definition | None) -> DefinitionFacts | None:
    if d is None:
        return None
    return DefinitionFacts(
        description=d.desc,
        system=d.system,
        severity=d.severity,
        stop_safely=d.stop_safely,
        causes=d.causes,
        escalation=d.escalation,
    )


def _notes(code: str, d: dd.Definition | None) -> tuple[Note, ...]:
    return tuple(Note(title=n.title, text=n.text) for n in relevant(code, d))


def generic_context(code: str, status: str) -> DtcContext:
    """A typical petrol car: no vehicle, freeze frame, other codes or usage."""
    d = dd.get(code)
    return DtcContext(code=code, status=status, definition=facts(d), notes=_notes(code, d))


def usage_bands(km: float, days: float, idle_s: float, driving_s: float, brakes: int):
    """Coarse bands (stable, so the cache key doesn't change with every trip). Braking uses
    the same 5 / 10 per 100 km bands as the wear multipliers (app/config/wear.yaml)."""
    km_day = km / days
    km_band = "under 20" if km_day < 20 else ("20 to 50" if km_day <= 50 else "over 50")
    engine_s = idle_s + driving_s
    idle = idle_s / engine_s if engine_s else 0.0
    idle_band = (
        "low (under 30 %)" if idle < 0.30
        else "typical (30 to 50 %)" if idle <= 0.50
        else "high (over 50 %)"
    )  # fmt: skip
    bands = [b.above for b in wear().multipliers["harsh_braking"].bands]
    per_100 = brakes / km * 100 if km > 0 else 0.0
    braking = "gentle" if per_100 <= bands[0] else ("moderate" if per_100 <= bands[-1] else "hard")
    return UsageFacts(km_per_day=km_band, idle_share=idle_band, braking=braking)


async def _usage(session, vehicle_id) -> UsageFacts | None:
    since = datetime.now(UTC) - timedelta(days=USAGE_WINDOW_DAYS)
    km, idle, driving, brakes, n = (
        await session.execute(
            select(
                func.coalesce(func.sum(Trip.distance_km), 0.0),
                func.coalesce(func.sum(Trip.idle_s), 0),
                func.coalesce(func.sum(Trip.driving_s), 0),
                func.coalesce(func.sum(Trip.harsh_brake_n), 0),
                func.count(),
            ).where(
                Trip.vehicle_id == vehicle_id,
                Trip.started_at >= since,
                Trip.summarised_at.is_not(None),
            )
        )
    ).one()
    if n == 0:
        return None
    return usage_bands(float(km), USAGE_WINDOW_DAYS, float(idle), float(driving), int(brakes))


async def _other_codes(session, vehicle_id, code: str) -> tuple[OtherCode, ...]:
    rows = (
        await session.execute(
            select(DtcEvent.code, DtcEvent.status).where(
                DtcEvent.vehicle_id == vehicle_id,
                DtcEvent.cleared_at.is_(None),
                DtcEvent.code != code,
            )
        )
    ).all()
    out = []
    for c, status in sorted({(c, s.value) for c, s in rows}):
        d = dd.get(c)
        out.append(OtherCode(code=c, status=status, description=d.desc if d else None))
    return tuple(out)


async def build_context(session, vehicle: Vehicle, event: DtcEvent) -> DtcContext:
    d = dd.get(event.code)
    frame = None
    if event.freeze_frame:
        frame = {k: float(v) for k, v in sorted(event.freeze_frame.items())}
    return DtcContext(
        code=event.code,
        status=event.status.value,
        definition=facts(d),
        vehicle=VehicleFacts(
            make=vehicle.make, model=vehicle.model, year=vehicle.year, engine=vehicle.engine
        ),  # fmt: skip
        freeze_frame=frame,
        other_active_codes=await _other_codes(session, vehicle.id, event.code),
        usage=await _usage(session, vehicle.id),
        notes=_notes(event.code, d),
    )

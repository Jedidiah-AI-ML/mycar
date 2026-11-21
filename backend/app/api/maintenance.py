"""Layer 1 maintenance API (Phase 3 step 7).

- `GET  /vehicles/{id}/maintenance` (viewer+): every service item with status, due date,
  km left and why; plus the odometer estimate, km/day and the wear metrics behind any
  multiplier. Computed on read (ADR-011): nothing stored, never stale, no timer.
- `POST/GET/DELETE /vehicles/{id}/service-records` (manager+ to write, viewer+ to read).
  A record with an odometer value also checks it against our estimate as of that date.
- `POST /vehicles/{id}/odometer` (manager+): an odometer reading off the dash.
- `GET  /vehicles/{id}/trips` (viewer+): trip summaries, newest first.
Writes are audited.
"""

from datetime import UTC, date, datetime, time, timedelta
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, Response, status
from sqlalchemy import func, select

from app.api.deps import AsManager, AsViewer, SessionDep
from app.models import OdometerReading, ServiceItem, ServiceRecord, Trip
from app.schemas.maintenance import (
    ItemOut,
    MaintenanceOut,
    MismatchOut,
    OdometerIn,
    OdometerOut,
    OdometerSummary,
    ServiceRecordCreated,
    ServiceRecordIn,
    ServiceRecordOut,
    TripOut,
    UsageSummary,
    WearSummary,
)
from app.services import audit
from app.services.maintenance import LastService, forecast_all
from app.services.service_rules import resolve, rules, sync_to_db, wear, wear_factors
from app.services.usage import LAGOS, record_odometer, usage_for

router = APIRouter(prefix="/vehicles/{vehicle_id}", tags=["maintenance"])


def lagos_today() -> date:
    return datetime.now(LAGOS).date()


def day_start(d: date) -> datetime:
    return datetime.combine(d, time(), LAGOS).astimezone(UTC)


def _mismatch_out(m) -> MismatchOut | None:
    if m is None:
        return None
    return MismatchOut(entered_km=m.entered_km, estimate_km=m.estimate_km, diff_km=m.diff_km,
                       threshold_km=m.threshold_km, flagged=m.flagged)  # fmt: skip


# ---------------------------------------------------------------- the forecast


async def _last_services(session, vehicle_id) -> dict[str, ServiceRecord]:
    """The newest record per item (a car has a handful of records: picked in Python)."""
    rows = (
        await session.scalars(
            select(ServiceRecord)
            .where(ServiceRecord.vehicle_id == vehicle_id)
            .order_by(ServiceRecord.done_on.desc(), ServiceRecord.id.desc())
        )
    ).all()
    newest: dict[str, ServiceRecord] = {}
    for r in rows:
        newest.setdefault(r.item_key, r)
    return newest


async def _km_since(session, vehicle_id, since: date) -> float:
    km = await session.scalar(
        select(func.coalesce(func.sum(Trip.distance_km), 0.0)).where(
            Trip.vehicle_id == vehicle_id, Trip.started_at >= day_start(since)
        )
    )
    return float(km)


async def _wear_window(session, vehicle_id, days: int) -> WearSummary:
    since = datetime.now(UTC) - timedelta(days=days)
    km, brakes, accels, idle, driving = (
        await session.execute(
            select(
                func.coalesce(func.sum(Trip.distance_km), 0.0),
                func.coalesce(func.sum(Trip.harsh_brake_n), 0),
                func.coalesce(func.sum(Trip.harsh_accel_n), 0),
                func.coalesce(func.sum(Trip.idle_s), 0),
                func.coalesce(func.sum(Trip.driving_s), 0),
            ).where(
                Trip.vehicle_id == vehicle_id,
                Trip.started_at >= since,
                Trip.summarised_at.is_not(None),
            )
        )
    ).one()
    km = float(km)
    engine_s = float(idle) + float(driving)
    return WearSummary(
        window_days=days,
        km_in_window=round(km, 1),
        harsh_brakes_per_100km=round(brakes / km * 100, 2) if km > 0 else None,
        harsh_accels_per_100km=round(accels / km * 100, 2) if km > 0 else None,
        idle_share=round(float(idle) / engine_s, 3) if engine_s > 0 else None,
    )


@router.get("/maintenance", response_model=MaintenanceOut)
async def maintenance(ctx: AsViewer, session: SessionDep):
    v = ctx.vehicle
    today = lagos_today()
    resolved = resolve(v.make, v.model, v.engine, v.oil_type)
    usage = await usage_for(session, v)
    records = await _last_services(session, v.id)
    last = {k: LastService(r.done_on, r.odometer_km, r.before_install) for k, r in records.items()}
    from_trips = {
        k: await _km_since(session, v.id, r.done_on)
        for k, r in records.items()
        if r.odometer_km is None and not r.before_install
    }
    w = await _wear_window(session, v.id, wear().window_days)
    metrics = {
        "harsh_brakes_per_100km": w.harsh_brakes_per_100km,
        "harsh_accels_per_100km": w.harsh_accels_per_100km,
        "idle_share": w.idle_share,
    }
    factors = wear_factors({k: x for k, x in metrics.items() if x is not None}, w.km_in_window)
    forecasts = forecast_all(
        resolved, last, today=today, estimate_km=usage.estimate.km,
        km_per_day=usage.km_per_day.value, km_from_trips_since=from_trips, wear=factors,
    )  # fmt: skip
    newest_user = await session.scalar(
        select(OdometerReading.mismatch)
        .where(OdometerReading.vehicle_id == v.id, OdometerReading.source == "user")
        .order_by(OdometerReading.ts.desc(), OdometerReading.id.desc())
        .limit(1)
    )
    est = usage.estimate
    return MaintenanceOut(
        vehicle_id=v.id,
        today=today,
        odometer=OdometerSummary(
            estimate_km=est.km,
            anchor_source=est.anchor.source if est.anchor else None,
            anchor_at=est.anchor.at if est.anchor else None,
            km_since_anchor=round(est.km_since_anchor, 1),
            partial_trips=est.partial_trips,
            last_entry_mismatch=bool(newest_user),
        ),
        usage=UsageSummary(
            km_per_day=round(usage.km_per_day.value, 1),
            method=usage.km_per_day.method,
            days_of_data=round(usage.km_per_day.days_of_data, 1),
        ),
        wear=w,
        items=[
            ItemOut(
                item=f.item,
                name=f.name,
                status=f.status,
                due_date=f.due_date,
                due_by=f.due_by,
                days_left=f.days_left,
                remaining_km=f.remaining_km,
                interval_km=f.interval_km,
                base_interval_km=f.base_interval_km,
                interval_months=f.interval_months,
                wear_factor=round(f.wear_factor, 3),
                rule_scope=resolved[f.item].scope,
                oil_variant=resolved[f.item].variant,
                oil_variant_assumed=resolved[f.item].assumed_variant,
                last_service=f.last_service,
                reasons=list(f.reasons),
            )
            for f in forecasts
        ],  # fmt: skip
    )


# ---------------------------------------------------------------- service records


@router.post(
    "/service-records", response_model=ServiceRecordCreated, status_code=status.HTTP_201_CREATED
)
async def add_service_record(body: ServiceRecordIn, ctx: AsManager, session: SessionDep):
    v = ctx.vehicle
    if body.item_key not in rules().items:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            f"Unknown service item. Use one of: {', '.join(rules().items)}",
        )
    if body.done_on > lagos_today():
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "done_on can't be in the future")
    if body.done_on < date(1990, 1, 1):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "done_on is implausibly old")
    if await session.get(ServiceItem, body.item_key) is None:
        await sync_to_db(session)  # first write after a deploy: copy the rules into the DB
    rec = ServiceRecord(vehicle_id=v.id, created_by=ctx.user.id, **body.model_dump())
    session.add(rec)
    await session.flush()
    check = None
    if body.odometer_km is not None:
        _, check = await record_odometer(
            session, v, body.odometer_km, "service", ctx.user.id,
            at=day_start(body.done_on), service_record_id=rec.id,
        )  # fmt: skip
    await audit.record(
        session, ctx.user.id, "service.recorded", "vehicle", v.id, record_id=rec.id,
        item=body.item_key, done_on=body.done_on.isoformat(),
    )  # fmt: skip
    await session.commit()
    await session.refresh(rec)
    out = ServiceRecordCreated.model_validate(rec, from_attributes=True)
    out.odometer_check = _mismatch_out(check)
    return out


@router.get("/service-records", response_model=list[ServiceRecordOut])
async def list_service_records(ctx: AsViewer, session: SessionDep):
    return (
        await session.scalars(
            select(ServiceRecord)
            .where(ServiceRecord.vehicle_id == ctx.vehicle.id)
            .order_by(ServiceRecord.done_on.desc(), ServiceRecord.id.desc())
        )
    ).all()


@router.delete("/service-records/{record_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_service_record(record_id: int, ctx: AsManager, session: SessionDep) -> Response:
    rec = await session.get(ServiceRecord, record_id)
    if rec is None or rec.vehicle_id != ctx.vehicle.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Service record not found")
    await session.delete(rec)  # its odometer value goes with it (ON DELETE CASCADE)
    await audit.record(
        session, ctx.user.id, "service.deleted", "vehicle", ctx.vehicle.id, record_id=record_id
    )
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ---------------------------------------------------------------- odometer


@router.post("/odometer", response_model=OdometerOut, status_code=status.HTTP_201_CREATED)
async def add_odometer_reading(body: OdometerIn, ctx: AsManager, session: SessionDep):
    reading, check = await record_odometer(session, ctx.vehicle, body.km, "user", ctx.user.id)
    if check is None:
        message = "Recorded. It's now the starting point for the distance estimate."
    elif check.flagged:
        message = (
            f"Recorded, but it differs from our estimate by {abs(check.diff_km):,} km (more "
            f"than {check.threshold_km:,} km). We'll keep counting distance from the last "
            "trusted reading. If the dash is right, check the install odometer."
        )
    else:
        message = "Recorded. It agrees with our estimate and is now the starting point."
    await audit.record(
        session, ctx.user.id, "odometer.recorded", "vehicle", ctx.vehicle.id, km=body.km,
        mismatch=reading.mismatch,
    )  # fmt: skip
    await session.commit()
    await session.refresh(reading)
    return OdometerOut(
        id=reading.id, km=reading.km, source=reading.source, ts=reading.ts,
        estimate_km_at_entry=reading.estimate_km_at_entry, mismatch=reading.mismatch,
        check=_mismatch_out(check), message=message,
    )  # fmt: skip


# ---------------------------------------------------------------- trips


@router.get("/trips", response_model=list[TripOut])
async def list_trips(
    ctx: AsViewer,
    session: SessionDep,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    before: Annotated[
        datetime | None, Query(description="Page: trips that started earlier")
    ] = None,
):
    stmt = select(Trip).where(Trip.vehicle_id == ctx.vehicle.id, Trip.started_at.is_not(None))
    if before is not None:
        stmt = stmt.where(Trip.started_at < before)
    return (await session.scalars(stmt.order_by(Trip.started_at.desc()).limit(limit))).all()

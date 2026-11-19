"""Admin-only endpoints. `is_admin` is set by hand in the database, never via the API."""

import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict
from sqlalchemy import func, select, tuple_

from app.api.deps import CurrentUser, SessionDep
from app.jobs.queue import enqueue
from app.models import (
    DeviceBatch,
    DtcEvent,
    Job,
    JobStatus,
    Trip,
    User,
    Vehicle,
    positions,
    readings,
)


async def require_admin(user: CurrentUser) -> User:
    if not user.is_admin:
        # 404 rather than 403: don't advertise that admin routes exist.
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found")
    return user


AdminUser = Annotated[User, Depends(require_admin)]
router = APIRouter(prefix="/admin", tags=["admin"])


class JobOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    status: JobStatus
    attempts: int
    last_error: str | None
    created_at: datetime
    finished_at: datetime | None


@router.post("/jobs/ping", response_model=JobOut, status_code=status.HTTP_202_ACCEPTED)
async def enqueue_ping(admin: AdminUser, session: SessionDep):
    job_id = await enqueue(session, "system.ping", {"requested_by": str(admin.id)})
    await session.commit()
    return await session.get(Job, job_id)


@router.get("/jobs/{job_id}", response_model=JobOut)
async def get_job_status(job_id: int, admin: AdminUser, session: SessionDep):
    job = await session.get(Job, job_id)
    if job is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Job not found")
    return job


class TelemetryStats(BaseModel):
    vehicle_id: uuid.UUID
    readings: int
    duplicate_readings: int  # rows identical in every column: must always be 0
    positions: int
    batches: int
    trips: dict[str, int]  # status -> count
    dtc_codes: list[str]


@router.get("/vehicles/{vehicle_id}/telemetry", response_model=TelemetryStats)
async def telemetry_stats(vehicle_id: uuid.UUID, admin: AdminUser, session: SessionDep):
    """What is stored for one car. Used by the live smoke test (plan step 11) to check
    ingestion without direct database access."""
    if await session.get(Vehicle, vehicle_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Vehicle not found")
    total, distinct = (
        await session.execute(
            select(
                func.count(),
                func.count(
                    func.distinct(
                        tuple_(readings.c.trip_id, readings.c.ts, readings.c.signal_id,
                               readings.c.value)
                    )
                ),
            ).where(readings.c.vehicle_id == vehicle_id)
        )
    ).one()  # fmt: skip
    n_pos = await session.scalar(
        select(func.count()).select_from(positions).where(positions.c.vehicle_id == vehicle_id)
    )
    trips = dict(
        (s.value, n)
        for s, n in (
            await session.execute(
                select(Trip.status, func.count()).where(Trip.vehicle_id == vehicle_id)
                .group_by(Trip.status)
            )
        ).all()
    )  # fmt: skip
    trip_ids = select(Trip.id).where(Trip.vehicle_id == vehicle_id)
    n_batches = await session.scalar(
        select(func.count()).select_from(DeviceBatch).where(DeviceBatch.trip_id.in_(trip_ids))
    )
    codes = (
        await session.scalars(
            select(DtcEvent.code).where(DtcEvent.vehicle_id == vehicle_id).distinct()
        )
    ).all()
    return TelemetryStats(
        vehicle_id=vehicle_id, readings=total, duplicate_readings=total - distinct,
        positions=n_pos, batches=n_batches, trips=trips, dtc_codes=sorted(codes),
    )  # fmt: skip

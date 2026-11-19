"""Vehicle CRUD. Every per-vehicle route goes through `require_vehicle_role`."""

from datetime import UTC, datetime

from fastapi import APIRouter, HTTPException, Response, status
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from app.api.deps import AsManager, AsOwner, AsViewer, CurrentUser, SessionDep
from app.models import DeviceAssignment, Vehicle, VehicleAccess, VehicleRole
from app.schemas.vehicle import VehicleCreate, VehicleOut, VehicleUpdate, VehicleWithRole
from app.services import audit
from app.services.vin import vin_warnings

router = APIRouter(prefix="/vehicles", tags=["vehicles"])

_VIN_TAKEN = HTTPException(status.HTTP_409_CONFLICT, "Another active vehicle already has this VIN")
_NOT_NULLABLE = {"make", "model", "year"}


def _out(vehicle: Vehicle, role: VehicleRole) -> VehicleWithRole:
    return VehicleWithRole(
        **VehicleOut.model_validate(vehicle).model_dump(),
        my_role=role,
        warnings=vin_warnings(vehicle.vin),
    )


@router.post("", response_model=VehicleWithRole, status_code=status.HTTP_201_CREATED)
async def create_vehicle(body: VehicleCreate, user: CurrentUser, session: SessionDep):
    vehicle = Vehicle(**body.model_dump(), created_by=user.id)
    session.add(vehicle)
    try:
        await session.flush()
    except IntegrityError:
        raise _VIN_TAKEN from None
    session.add(
        VehicleAccess(
            vehicle_id=vehicle.id, user_id=user.id, role=VehicleRole.owner, granted_by=user.id
        )
    )
    await audit.record(session, user.id, "vehicle.created", "vehicle", vehicle.id)
    await session.commit()
    await session.refresh(vehicle)
    return _out(vehicle, VehicleRole.owner)


@router.get("", response_model=list[VehicleWithRole])
async def list_my_vehicles(user: CurrentUser, session: SessionDep):
    rows = (
        await session.execute(
            select(Vehicle, VehicleAccess.role)
            .join(VehicleAccess, VehicleAccess.vehicle_id == Vehicle.id)
            .where(
                VehicleAccess.user_id == user.id,
                VehicleAccess.revoked_at.is_(None),
                Vehicle.deleted_at.is_(None),
            )
            .order_by(Vehicle.created_at)
        )
    ).all()
    return [_out(v, role) for v, role in rows]


@router.get("/{vehicle_id}", response_model=VehicleWithRole)
async def get_vehicle(ctx: AsViewer):
    return _out(ctx.vehicle, ctx.role)


@router.patch("/{vehicle_id}", response_model=VehicleWithRole)
async def update_vehicle(body: VehicleUpdate, ctx: AsManager, session: SessionDep):
    changes = body.model_dump(exclude_unset=True)
    nulled = sorted(k for k in _NOT_NULLABLE if k in changes and changes[k] is None)
    if nulled:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, f"These fields can't be empty: {nulled}"
        )
    for k, v in changes.items():
        setattr(ctx.vehicle, k, v)
    try:
        await session.flush()
    except IntegrityError:
        raise _VIN_TAKEN from None
    await audit.record(
        session, ctx.user.id, "vehicle.updated", "vehicle", ctx.vehicle.id, fields=sorted(changes)
    )
    await session.commit()
    await session.refresh(ctx.vehicle)
    return _out(ctx.vehicle, ctx.role)


@router.delete("/{vehicle_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_vehicle(ctx: AsOwner, session: SessionDep) -> Response:
    now = datetime.now(UTC)
    ctx.vehicle.deleted_at = now
    await session.execute(
        update(VehicleAccess)
        .where(VehicleAccess.vehicle_id == ctx.vehicle.id, VehicleAccess.revoked_at.is_(None))
        .values(revoked_at=now)
    )
    # Free the car's device so it can be claimed again (e.g. moved to the next car).
    freed = await session.scalar(
        update(DeviceAssignment)
        .where(
            DeviceAssignment.vehicle_id == ctx.vehicle.id, DeviceAssignment.unassigned_at.is_(None)
        )
        .values(unassigned_at=now)
        .returning(DeviceAssignment.device_id)
    )
    await audit.record(
        session, ctx.user.id, "vehicle.deleted", "vehicle", ctx.vehicle.id,
        **({"device_unclaimed": freed} if freed else {}),
    )  # fmt: skip
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)

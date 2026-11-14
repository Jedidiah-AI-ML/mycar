"""Shared access to a vehicle (PRD 6.11). Every change is written to the audit trail.

Rules:
- Only the owner shares, changes roles, or removes people.
- Shared roles are `viewer` or `manager`; there is always exactly one owner.
- Anyone who isn't the owner may remove *themselves* (leave a shared car).
- Managers can see who has access; viewers can't (other people's emails are private).
"""

import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, HTTPException, Response, status
from sqlalchemy import func, select

from app.api.deps import AsManager, AsOwner, AsViewer, SessionDep
from app.models import User, VehicleAccess, VehicleRole
from app.schemas.vehicle import AccessGrant, AccessOut, AccessUpdate
from app.services import audit

router = APIRouter(prefix="/vehicles/{vehicle_id}/access", tags=["sharing"])


async def _active(session, vehicle_id: uuid.UUID, user_id: uuid.UUID) -> VehicleAccess | None:
    return await session.scalar(
        select(VehicleAccess).where(
            VehicleAccess.vehicle_id == vehicle_id,
            VehicleAccess.user_id == user_id,
            VehicleAccess.revoked_at.is_(None),
        )
    )


def _access_out(a: VehicleAccess, u: User) -> AccessOut:
    return AccessOut(
        user_id=u.id,
        email=u.email,
        display_name=u.display_name,
        role=a.role,
        granted_at=a.granted_at,
        granted_by=a.granted_by,
    )


@router.get("", response_model=list[AccessOut])
async def list_access(ctx: AsManager, session: SessionDep):
    rows = (
        await session.execute(
            select(VehicleAccess, User)
            .join(User, User.id == VehicleAccess.user_id)
            .where(VehicleAccess.vehicle_id == ctx.vehicle.id, VehicleAccess.revoked_at.is_(None))
            .order_by(VehicleAccess.granted_at)
        )
    ).all()
    return [_access_out(a, u) for a, u in rows]


@router.post("", response_model=AccessOut, status_code=status.HTTP_201_CREATED)
async def share(body: AccessGrant, ctx: AsOwner, session: SessionDep):
    target = await session.scalar(
        select(User).where(func.lower(User.email) == body.email.lower(), User.deleted_at.is_(None))
    )
    if target is None:
        # Phase 1 shares only with existing accounts; email invites come in Phase 6.
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            "No AutoCare account uses that email. Ask them to sign up first.",
        )
    if await _active(session, ctx.vehicle.id, target.id):
        raise HTTPException(status.HTTP_409_CONFLICT, "That person already has access")

    a = VehicleAccess(
        vehicle_id=ctx.vehicle.id,
        user_id=target.id,
        role=VehicleRole(body.role),
        granted_by=ctx.user.id,
    )
    session.add(a)
    await session.flush()
    await audit.record(
        session,
        ctx.user.id,
        "access.granted",
        "vehicle",
        ctx.vehicle.id,
        user_id=target.id,
        role=body.role,
    )
    await session.commit()
    await session.refresh(a)
    return _access_out(a, target)


@router.patch("/{user_id}", response_model=AccessOut)
async def change_role(user_id: uuid.UUID, body: AccessUpdate, ctx: AsOwner, session: SessionDep):
    a = await _active(session, ctx.vehicle.id, user_id)
    if a is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "That person doesn't have access")
    if a.role == VehicleRole.owner:
        raise HTTPException(status.HTTP_409_CONFLICT, "The owner's role can't be changed")
    old = a.role
    a.role = VehicleRole(body.role)
    await audit.record(
        session,
        ctx.user.id,
        "access.role_changed",
        "vehicle",
        ctx.vehicle.id,
        user_id=user_id,
        old=old.value,
        new=body.role,
    )
    await session.commit()
    target = await session.get(User, user_id)
    assert target is not None
    return _access_out(a, target)


@router.delete("/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_access(user_id: uuid.UUID, ctx: AsViewer, session: SessionDep) -> Response:
    leaving = user_id == ctx.user.id
    if not leaving and ctx.role != VehicleRole.owner:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Only the owner can remove other people")
    a = await _active(session, ctx.vehicle.id, user_id)
    if a is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "That person doesn't have access")
    if a.role == VehicleRole.owner:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "The owner can't be removed. Delete the vehicle instead.",
        )
    a.revoked_at = datetime.now(UTC)
    await audit.record(
        session,
        ctx.user.id,
        "access.left" if leaving else "access.revoked",
        "vehicle",
        ctx.vehicle.id,
        user_id=user_id,
    )
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)

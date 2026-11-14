"""Shared FastAPI dependencies: current user and per-vehicle authorisation."""

import uuid
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import InvalidToken, decode_access_token
from app.db.session import get_session
from app.models import User, Vehicle, VehicleAccess, VehicleRole

_bearer = HTTPBearer(auto_error=False)

SessionDep = Annotated[AsyncSession, Depends(get_session)]


def _unauthorised(detail: str = "Not authenticated") -> HTTPException:
    return HTTPException(
        status.HTTP_401_UNAUTHORIZED, detail, headers={"WWW-Authenticate": "Bearer"}
    )


async def current_user(
    session: SessionDep,
    creds: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> User:
    if creds is None:
        raise _unauthorised()
    try:
        user_id = decode_access_token(creds.credentials)
    except InvalidToken:
        raise _unauthorised("Invalid or expired token") from None
    user = await session.get(User, user_id)
    if user is None or user.deleted_at is not None:
        raise _unauthorised("Invalid or expired token")
    return user


CurrentUser = Annotated[User, Depends(current_user)]


@dataclass
class VehicleContext:
    vehicle: Vehicle
    role: VehicleRole
    user: User


def require_vehicle_role(min_role: VehicleRole):
    """Dependency factory used by every `/vehicles/{vehicle_id}/...` route.

    - No active access (or the vehicle is deleted): 404, so vehicle IDs can't be probed.
    - Access, but the role is too low: 403.
    """

    async def dep(vehicle_id: uuid.UUID, user: CurrentUser, session: SessionDep) -> VehicleContext:
        row = (
            await session.execute(
                select(Vehicle, VehicleAccess.role)
                .join(VehicleAccess, VehicleAccess.vehicle_id == Vehicle.id)
                .where(
                    Vehicle.id == vehicle_id,
                    Vehicle.deleted_at.is_(None),
                    VehicleAccess.user_id == user.id,
                    VehicleAccess.revoked_at.is_(None),
                )
            )
        ).first()
        if row is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Vehicle not found")
        vehicle, role = row
        if not role.at_least(min_role):
            raise HTTPException(
                status.HTTP_403_FORBIDDEN, f"This needs the {min_role.value} role or higher"
            )
        return VehicleContext(vehicle=vehicle, role=role, user=user)

    return dep


AsViewer = Annotated[VehicleContext, Depends(require_vehicle_role(VehicleRole.viewer))]
AsManager = Annotated[VehicleContext, Depends(require_vehicle_role(VehicleRole.manager))]
AsOwner = Annotated[VehicleContext, Depends(require_vehicle_role(VehicleRole.owner))]

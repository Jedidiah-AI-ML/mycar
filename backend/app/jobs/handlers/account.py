"""Permanent deletion of an account after the grace period (NDPA, PRD 10)."""

import logging
import uuid

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.jobs.registry import job
from app.models import User, Vehicle, VehicleAccess, VehicleRole
from app.services import audit

log = logging.getLogger("app.jobs.account")


@job("account.purge", max_attempts=5)
async def purge_account(payload: dict, session: AsyncSession) -> None:
    user_id = uuid.UUID(payload["user_id"])
    user = await session.get(User, user_id)
    if user is None or user.deleted_at is None:
        log.info("nothing to purge", extra={"user_id": str(user_id)})
        return

    # Vehicles this user owned (already soft-deleted by DELETE /me) go too, with
    # everyone's access rows (ON DELETE CASCADE).
    owned = select(VehicleAccess.vehicle_id).where(
        VehicleAccess.user_id == user_id, VehicleAccess.role == VehicleRole.owner
    )
    await session.execute(
        delete(Vehicle).where(Vehicle.id.in_(owned), Vehicle.deleted_at.is_not(None))
    )
    # Consents, refresh tokens and remaining access rows cascade from the user row.
    await session.delete(user)
    # The audit trail keeps only the (now meaningless) UUID, no personal data.
    await audit.record(session, None, "account.purged", "user", user_id)
    log.info("account purged", extra={"user_id": str(user_id)})

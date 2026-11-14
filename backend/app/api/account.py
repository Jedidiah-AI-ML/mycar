"""The signed-in user's own account: profile, consents, data export, deletion (NDPA)."""

from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, HTTPException, Response, status
from sqlalchemy import select, update

from app.api.deps import CurrentUser, SessionDep
from app.core.config import get_settings
from app.jobs.queue import enqueue
from app.models import (
    AuditEvent,
    Consent,
    ConsentPurpose,
    RefreshToken,
    Vehicle,
    VehicleAccess,
    VehicleRole,
)
from app.schemas.account import ConsentRecord, ConsentsOut, ConsentStatus
from app.schemas.auth import UserOut
from app.schemas.vehicle import VehicleOut
from app.services import audit, consents

router = APIRouter(prefix="/me", tags=["account"])


@router.get("", response_model=UserOut)
async def me(user: CurrentUser) -> object:
    return user


# ---------------------------------------------------------------- consents


@router.get("/consents", response_model=ConsentsOut)
async def list_consents(user: CurrentUser, session: SessionDep) -> ConsentsOut:
    cfg = consents.consent_config()
    rows = (
        await session.scalars(
            select(Consent).where(Consent.user_id == user.id).order_by(Consent.granted_at)
        )
    ).all()
    active = {c.purpose: c for c in rows if c.revoked_at is None}
    purposes = []
    for p in ConsentPurpose:
        meta, c = cfg["purposes"][p.value], active.get(p)
        purposes.append(
            ConsentStatus(
                purpose=p,
                title=meta["title"],
                text=meta["text"],
                required=meta["required"],
                active=c is not None,
                granted_version=c.version if c else None,
                needs_reconsent=bool(c and c.version != cfg["version"]),
            )
        )
    return ConsentsOut(
        current_version=cfg["version"],
        purposes=purposes,
        history=[ConsentRecord.model_validate(c) for c in rows],
    )


@router.post(
    "/consents/{purpose}", response_model=ConsentRecord, status_code=status.HTTP_201_CREATED
)
async def grant_consent(purpose: ConsentPurpose, user: CurrentUser, session: SessionDep) -> object:
    c = await consents.grant(session, user.id, purpose)
    await session.commit()
    return c


@router.delete("/consents/{purpose}", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_consent(
    purpose: ConsentPurpose, user: CurrentUser, session: SessionDep
) -> Response:
    if purpose in consents.required_purposes():
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "AutoCare can't run without this consent. To withdraw it, delete your account "
            "(DELETE /me), which removes your data.",
        )
    if not await consents.revoke(session, user.id, purpose):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No active consent for this purpose")
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ---------------------------------------------------------------- export & delete


@router.get("/export")
async def export_my_data(user: CurrentUser, session: SessionDep) -> dict:
    """Everything AutoCare holds about this user (grows as later phases add data)."""
    consent_rows = await session.scalars(select(Consent).where(Consent.user_id == user.id))
    access_rows = (
        await session.execute(
            select(VehicleAccess, Vehicle)
            .join(Vehicle, Vehicle.id == VehicleAccess.vehicle_id)
            .where(VehicleAccess.user_id == user.id, Vehicle.deleted_at.is_(None))
        )
    ).all()
    audit_rows = await session.scalars(
        select(AuditEvent).where(AuditEvent.actor_user_id == user.id).order_by(AuditEvent.id)
    )
    return {
        "exported_at": datetime.now(UTC).isoformat(),
        "user": UserOut.model_validate(user).model_dump(mode="json"),
        "consents": [ConsentRecord.model_validate(c).model_dump(mode="json") for c in consent_rows],
        "vehicles": [
            {
                "role": a.role.value,
                "access_granted_at": a.granted_at.isoformat(),
                "access_revoked_at": a.revoked_at.isoformat() if a.revoked_at else None,
                "vehicle": VehicleOut.model_validate(veh).model_dump(mode="json"),
            }
            for a, veh in access_rows
        ],
        "activity": [
            {
                "at": e.created_at.isoformat(),
                "action": e.action,
                "subject": f"{e.subject_type}:{e.subject_id}",
                "details": e.details,
            }
            for e in audit_rows
        ],
    }


@router.delete("", status_code=status.HTTP_204_NO_CONTENT)
async def delete_my_account(user: CurrentUser, session: SessionDep) -> Response:
    """Soft-deletes now: login stops working and the user's vehicles disappear at once.
    The rows are purged permanently after `ACCOUNT_PURGE_DAYS` by a background job."""
    now = datetime.now(UTC)
    user.deleted_at = now

    # Vehicles this user owns are deleted for everyone (shared users lose access).
    owned_ids = select(VehicleAccess.vehicle_id).where(
        VehicleAccess.user_id == user.id,
        VehicleAccess.role == VehicleRole.owner,
        VehicleAccess.revoked_at.is_(None),
    )
    await session.execute(
        update(Vehicle)
        .where(Vehicle.id.in_(owned_ids), Vehicle.deleted_at.is_(None))
        .values(deleted_at=now)
    )
    await session.execute(
        update(VehicleAccess)
        .where(VehicleAccess.user_id == user.id, VehicleAccess.revoked_at.is_(None))
        .values(revoked_at=now)
    )
    await session.execute(
        update(RefreshToken)
        .where(RefreshToken.user_id == user.id, RefreshToken.revoked_at.is_(None))
        .values(revoked_at=now)
    )
    purge_at = now + timedelta(days=get_settings().account_purge_days)
    await enqueue(
        session,
        "account.purge",
        {"user_id": str(user.id)},
        run_at=purge_at,
        dedupe_key=f"account.purge:{user.id}",
    )
    await audit.record(
        session, user.id, "account.deleted", "user", user.id, purge_at=purge_at.isoformat()
    )
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)

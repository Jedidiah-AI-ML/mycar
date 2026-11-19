"""Device provisioning (admin), claiming a device for a car, and the car's device status.

- `POST /admin/devices`: create a device. Returns its upload token and claim code
  **once**; only hashes are stored (app.gateway.auth).
- `POST /vehicles/{id}/device` (owner/manager): link a device using the claim code
  from its label. One device per car, one car per device (PRD 7, DB-enforced).
- `DELETE /vehicles/{id}/device` (owner/manager): unlink. The history row stays.
- `GET /vehicles/{id}/device` (viewer+): online, last seen, signal, data used this
  month, effective capabilities.
Claims and unclaims go to the audit trail.
"""

import re
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, HTTPException, Response, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.api.admin import AdminUser
from app.api.deps import AsManager, AsViewer, SessionDep
from app.core.config import get_settings
from app.gateway import auth, capabilities
from app.models import ConsentPurpose, Device, DeviceAssignment
from app.schemas.device import DeviceClaim, DeviceProvision, DeviceProvisioned, DeviceStatus
from app.services import audit, consents

admin_router = APIRouter(prefix="/admin/devices", tags=["admin"])
router = APIRouter(prefix="/vehicles/{vehicle_id}/device", tags=["devices"])

# Same answer for "no such device" and "wrong code": device ids can't be probed.
_BAD_CLAIM = HTTPException(status.HTTP_403_FORBIDDEN, "Device ID or claim code is wrong")


# ---------------------------------------------------------------- admin: provision


async def _next_device_id(session) -> str:
    prefix = get_settings().device_id_prefix
    ids = (await session.scalars(select(Device.id).where(Device.id.like(f"{prefix}-%")))).all()
    nums = [int(m[1]) for i in ids if (m := re.fullmatch(rf"{prefix}-(\d+)", i))]
    return f"{prefix}-{max(nums, default=0) + 1:04d}"


@admin_router.post("", response_model=DeviceProvisioned, status_code=status.HTTP_201_CREATED)
async def provision_device(body: DeviceProvision, admin: AdminUser, session: SessionDep):
    device_id = body.id or await _next_device_id(session)
    secret, code = auth.new_device_secret(), auth.new_claim_code()
    session.add(
        Device(
            id=device_id,
            type=body.type,
            token_hash=auth.hash_secret(secret),
            claim_code_hash=auth.hash_claim_code(code),
            sim_iccid=body.sim_iccid,
        )
    )
    try:
        await session.flush()
    except IntegrityError:
        raise HTTPException(
            status.HTTP_409_CONFLICT, f"Device {device_id} already exists"
        ) from None
    await audit.record(session, admin.id, "device.provisioned", "device", device_id)
    await session.commit()
    return DeviceProvisioned(
        id=device_id,
        type=body.type,
        token=auth.device_token(device_id, secret),
        claim_code=code,
        label_qr=f"autocare:claim:{device_id}:{code}",
    )


# ---------------------------------------------------------------- claim / unclaim


async def _active_assignment(session, **where) -> DeviceAssignment | None:
    stmt = select(DeviceAssignment).where(DeviceAssignment.unassigned_at.is_(None))
    for col, value in where.items():
        stmt = stmt.where(getattr(DeviceAssignment, col) == value)
    return await session.scalar(stmt)


@router.post("", response_model=DeviceStatus, status_code=status.HTTP_201_CREATED)
async def claim_device(body: DeviceClaim, ctx: AsManager, session: SessionDep, response: Response):
    device = await session.get(Device, body.device_id)
    if not auth.claim_code_ok(device, body.claim_code) or device.disabled_at is not None:
        raise _BAD_CLAIM
    current = await _active_assignment(session, device_id=device.id)
    if current is not None and current.vehicle_id == ctx.vehicle.id:
        response.status_code = status.HTTP_200_OK  # already linked to this car: no-op
        return await _status(session, ctx, device, current)
    if current is not None:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "This device is linked to another car. Its owner must unlink it first.",
        )
    if await _active_assignment(session, vehicle_id=ctx.vehicle.id) is not None:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "This car already has a device. Unlink it first (one device per car).",
        )
    assignment = DeviceAssignment(
        device_id=device.id, vehicle_id=ctx.vehicle.id, assigned_by=ctx.user.id
    )
    session.add(assignment)
    try:
        await session.flush()
    except IntegrityError:  # lost a race with a concurrent claim
        raise HTTPException(status.HTTP_409_CONFLICT, "Device or car was just linked") from None
    await audit.record(
        session, ctx.user.id, "device.claimed", "vehicle", ctx.vehicle.id, device_id=device.id
    )
    await session.commit()
    await session.refresh(assignment)
    return await _status(session, ctx, device, assignment)


@router.delete("", status_code=status.HTTP_204_NO_CONTENT)
async def unclaim_device(ctx: AsManager, session: SessionDep) -> Response:
    current = await _active_assignment(session, vehicle_id=ctx.vehicle.id)
    if current is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "This car has no device")
    current.unassigned_at = datetime.now(UTC)
    await audit.record(
        session, ctx.user.id, "device.unclaimed", "vehicle", ctx.vehicle.id,
        device_id=current.device_id,
    )  # fmt: skip
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ---------------------------------------------------------------- status


@router.get("", response_model=DeviceStatus)
async def device_status(ctx: AsViewer, session: SessionDep):
    current = await _active_assignment(session, vehicle_id=ctx.vehicle.id)
    if current is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "This car has no device")
    device = await session.get(Device, current.device_id)
    return await _status(session, ctx, device, current)


def _signal(rssi: int | None) -> str:
    # SEED - validate: usual LTE RSSI bands (dBm)
    if rssi is None:
        return "unknown"
    if rssi >= -65:
        return "excellent"
    if rssi >= -75:
        return "good"
    if rssi >= -85:
        return "fair"
    return "poor"


async def _status(session, ctx, device: Device, assignment: DeviceAssignment) -> DeviceStatus:
    now = datetime.now(UTC)
    online_window = timedelta(seconds=get_settings().device_online_s)
    this_month = now.date().replace(day=1)
    used = device.bytes_month if device.bytes_month_start == this_month else 0
    return DeviceStatus(
        device_id=device.id,
        type=device.type,
        assigned_at=assignment.assigned_at,
        online=device.last_seen_at is not None and now - device.last_seen_at <= online_window,
        last_seen_at=device.last_seen_at,
        last_rssi=device.last_rssi,
        signal=_signal(device.last_rssi),
        firmware_version=device.firmware_version,
        data_used_this_month_bytes=used,
        capabilities=capabilities.effective(
            device.type,
            device.capabilities,
            await consents.owner_has_consent(session, ctx.vehicle.id, ConsentPurpose.location),
        ),
    )

"""Database-level rules that must hold even if application code has a bug."""

import pytest
from sqlalchemy.exc import IntegrityError

from app.models import Consent, ConsentPurpose, User, Vehicle, VehicleAccess, VehicleRole


async def make_user(session, email="a@example.com") -> User:
    u = User(email=email, password_hash="x", display_name="A")
    session.add(u)
    await session.flush()
    return u


async def make_vehicle(session, owner: User, **kw) -> Vehicle:
    fields = {"make": "Toyota", "model": "Corolla", "year": 2010} | kw
    v = Vehicle(created_by=owner.id, **fields)
    session.add(v)
    await session.flush()
    return v


async def test_email_unique_case_insensitive(session):
    await make_user(session, "Wisdom@Example.com")
    with pytest.raises(IntegrityError):
        await make_user(session, "wisdom@example.com")


async def test_one_active_consent_per_purpose(session):
    u = await make_user(session)
    session.add(Consent(user_id=u.id, purpose=ConsentPurpose.location, version="v1"))
    await session.flush()
    session.add(Consent(user_id=u.id, purpose=ConsentPurpose.location, version="v1"))
    with pytest.raises(IntegrityError):
        await session.flush()


async def test_one_active_owner_per_vehicle(session):
    a = await make_user(session, "a@x.com")
    b = await make_user(session, "b@x.com")
    v = await make_vehicle(session, a)
    session.add(VehicleAccess(vehicle_id=v.id, user_id=a.id, role=VehicleRole.owner))
    await session.flush()
    session.add(VehicleAccess(vehicle_id=v.id, user_id=b.id, role=VehicleRole.owner))
    with pytest.raises(IntegrityError):
        await session.flush()


@pytest.mark.parametrize(
    "kw",
    [
        {"fuel_type": "diesel"},
        {"year": 1995},
        {"odometer_at_install_km": -1},
        {"odometer_at_install_km": 1_500_001},
        {"cylinders": 0},
    ],
)
async def test_vehicle_checks(session, kw):
    u = await make_user(session)
    with pytest.raises(IntegrityError):
        await make_vehicle(session, u, **kw)


async def test_active_vin_unique(session):
    u = await make_user(session)
    await make_vehicle(session, u, vin="JTDBU4EE9A9123456")
    with pytest.raises(IntegrityError):
        await make_vehicle(session, u, vin="JTDBU4EE9A9123456")

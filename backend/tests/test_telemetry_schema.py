"""Phase 2 step 6: database rules for devices, trips and telemetry (migration 0002).

These must hold even if application code has a bug: one device per car, one car per
device, a batch key stored once, valid codes and ids.
"""

import hashlib
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, insert, select, text, update
from sqlalchemy.exc import DBAPIError, IntegrityError

from app.gateway.signals import catalogue, sync_signals
from app.models import (
    Device,
    DeviceAssignment,
    DeviceBatch,
    DeviceCommand,
    DeviceCommandStatus,
    DeviceCommandType,
    DtcEvent,
    DtcStatus,
    Mode06Result,
    Signal,
    Trip,
    TripStatus,
    positions,
    readings,
)
from tests.test_models import make_user, make_vehicle

NOW = datetime(2026, 11, 17, 12, 0, tzinfo=UTC)


def token_hash(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()


async def make_device(session, device_id="FM1-0001", **kw) -> Device:
    fields = {"type": "freematics_one_plus", "token_hash": token_hash(device_id)} | kw
    d = Device(id=device_id, **fields)
    session.add(d)
    await session.flush()
    return d


async def car(session, email="a@example.com", **kw):
    return await make_vehicle(session, await make_user(session, email), **kw)


# ---------------------------------------------------------------- devices


@pytest.mark.parametrize(
    "kw",
    [
        {"device_id": "fm1-0001"},  # lower case
        {"device_id": "F!"},
        {"type": "teltonika"},  # not a registered adapter (yet)
        {"token_hash": "not-a-sha256"},
    ],
)
async def test_device_checks(session, kw):
    with pytest.raises(IntegrityError):
        await make_device(session, **kw)


async def test_device_token_hash_unique(session):
    await make_device(session, "FM1-0001", token_hash=token_hash("same"))
    with pytest.raises(IntegrityError):
        await make_device(session, "FM1-0002", token_hash=token_hash("same"))


async def test_device_defaults(session):
    d = await make_device(session)
    await session.refresh(d)
    assert d.capabilities == {} and d.bytes_month == 0 and d.disabled_at is None


# ---------------------------------------------------------------- assignments


async def test_one_active_assignment_per_device(session):
    d = await make_device(session)
    v1 = await car(session, "a@x.com")
    v2 = await car(session, "b@x.com")
    session.add(DeviceAssignment(device_id=d.id, vehicle_id=v1.id))
    await session.flush()
    session.add(DeviceAssignment(device_id=d.id, vehicle_id=v2.id))
    with pytest.raises(IntegrityError):
        await session.flush()


async def test_one_active_assignment_per_vehicle(session):
    v = await car(session)
    d1 = await make_device(session, "FM1-0001")
    d2 = await make_device(session, "FM1-0002")
    session.add(DeviceAssignment(device_id=d1.id, vehicle_id=v.id))
    await session.flush()
    session.add(DeviceAssignment(device_id=d2.id, vehicle_id=v.id))
    with pytest.raises(IntegrityError):  # PRD 7: never a second dongle in the same car
        await session.flush()


async def test_reassignment_after_unassign_keeps_history(session):
    d = await make_device(session)
    v1 = await car(session, "a@x.com")
    v2 = await car(session, "b@x.com")
    first = DeviceAssignment(device_id=d.id, vehicle_id=v1.id, assigned_at=NOW)
    session.add(first)
    await session.flush()
    first.unassigned_at = NOW + timedelta(days=3)
    session.add(DeviceAssignment(device_id=d.id, vehicle_id=v2.id))
    await session.flush()
    n = await session.scalar(select(func.count()).select_from(DeviceAssignment))
    assert n == 2


async def test_unassigned_must_follow_assigned(session):
    d = await make_device(session)
    v = await car(session)
    session.add(
        DeviceAssignment(
            device_id=d.id, vehicle_id=v.id, assigned_at=NOW, unassigned_at=NOW - timedelta(1)
        )
    )
    with pytest.raises(IntegrityError):
        await session.flush()


# ---------------------------------------------------------------- batch key


async def test_batch_key_unique(session):
    await make_device(session)
    session.add(DeviceBatch(device_id="FM1-0001", boot=1, trip=7, seq=0, n_samples=300))
    await session.flush()
    session.add(DeviceBatch(device_id="FM1-0001", boot=1, trip=7, seq=1))  # next batch: fine
    session.add(DeviceBatch(device_id="FM1-0001", boot=2, trip=7, seq=0))  # new boot: fine
    await session.flush()
    session.add(DeviceBatch(device_id="FM1-0001", boot=1, trip=7, seq=0))  # retry
    with pytest.raises(IntegrityError):
        await session.flush()


async def test_batch_key_conflict_is_a_no_op_with_on_conflict(session):
    """How step 8 will use it: INSERT ... ON CONFLICT DO NOTHING tells a retry apart."""
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    await make_device(session)
    stmt = (
        pg_insert(DeviceBatch)
        .values(device_id="FM1-0001", boot=0, trip=1, seq=0)
        .on_conflict_do_nothing()
        .returning(DeviceBatch.seq)
    )
    assert (await session.execute(stmt)).first() is not None  # new
    assert (await session.execute(stmt)).first() is None  # duplicate


async def test_negative_counters_rejected(session):
    await make_device(session)
    session.add(DeviceBatch(device_id="FM1-0001", boot=-1, trip=0, seq=0))
    with pytest.raises(IntegrityError):
        await session.flush()


# ---------------------------------------------------------------- trips


async def test_trip_unique_per_device_counters(session):
    v = await car(session)
    await make_device(session)
    kw = {"vehicle_id": v.id, "device_id": "FM1-0001", "device_boot": 0, "device_trip": 5}
    t = Trip(**kw)
    session.add(t)
    await session.flush()
    await session.refresh(t)
    assert t.status == TripStatus.open and t.gaps == [] and t.n_samples == 0
    session.add(Trip(**kw))
    with pytest.raises(IntegrityError):
        await session.flush()


@pytest.mark.parametrize(
    "kw",
    [
        {"completeness": 1.5},
        {"started_at": NOW, "ended_at": NOW - timedelta(minutes=1)},
    ],
)
async def test_trip_checks(session, kw):
    v = await car(session)
    session.add(Trip(vehicle_id=v.id, device_boot=0, device_trip=1, **kw))
    with pytest.raises(IntegrityError):
        await session.flush()


# ---------------------------------------------------------------- signals + readings


async def test_signal_catalogue_sync_is_idempotent(session):
    n = await sync_signals(session)
    assert n == len(catalogue().signals)
    await session.execute(update(Signal).where(Signal.id == 1).values(unit="wrong"))
    assert await sync_signals(session) == n  # second run repairs, adds nothing
    assert await session.scalar(select(func.count()).select_from(Signal)) == n
    assert (await session.get(Signal, 1, populate_existing=True)).unit == "rpm"


async def test_readings_and_positions_store_and_index(session):
    v = await car(session)
    trip = uuid.uuid4()
    rows = [
        {"vehicle_id": v.id, "trip_id": trip, "ts": NOW + timedelta(seconds=i), "signal_id": 1,
         "value": 800.0 + i}
        for i in range(100)
    ]  # fmt: skip
    await session.execute(insert(readings), rows)
    await session.execute(
        insert(positions),
        [{"vehicle_id": v.id, "trip_id": trip, "ts": NOW, "lat": 6.5244, "lon": 3.3792,
          "speed_kmh": 24.0}],
    )  # fmt: skip
    got = await session.scalar(
        select(func.max(readings.c.value)).where(
            readings.c.trip_id == trip, readings.c.signal_id == 1
        )
    )
    assert got == pytest.approx(899.0)
    lat = await session.scalar(select(positions.c.lat))
    assert lat == 6.5244  # double precision: exact enough for a map (and for erasure checks)
    # The planned indexes exist: BRIN on time, B-tree for "one signal of one trip".
    idx = (
        await session.execute(
            text("SELECT indexname, indexdef FROM pg_indexes WHERE tablename = 'readings'")
        )
    ).all()
    defs = {name: d for name, d in idx}
    assert "USING brin (ts)" in defs["ix_readings_ts_brin"]
    assert "(trip_id, signal_id, ts)" in defs["ix_readings_trip_signal_ts"]


async def test_readings_value_is_4_byte_real(session):
    v = await car(session)
    await session.execute(
        insert(readings),
        [{"vehicle_id": v.id, "trip_id": None, "ts": NOW, "signal_id": 16, "value": 12.345678}],
    )
    typ = await session.scalar(text("SELECT pg_typeof(value)::text FROM readings LIMIT 1"))
    assert typ == "real"


async def test_readings_have_no_foreign_keys(session):
    """ADR-003: bulk COPY, bulk delete; nothing checks every row against vehicles."""
    n = await session.scalar(
        text(
            "SELECT count(*) FROM information_schema.table_constraints "
            "WHERE table_name IN ('readings', 'positions') AND constraint_type = 'FOREIGN KEY'"
        )
    )
    assert n == 0


# ---------------------------------------------------------------- DTCs, Mode 06, commands


async def test_dtc_code_format_and_one_active_row(session):
    v = await car(session)
    kw = {"vehicle_id": v.id, "first_seen": NOW, "last_seen": NOW}
    session.add(DtcEvent(code="P0171", status=DtcStatus.pending, **kw))
    session.add(DtcEvent(code="P0171", status=DtcStatus.stored, **kw))  # other status: fine
    await session.flush()
    session.add(DtcEvent(code="P0171", status=DtcStatus.pending, **kw))
    with pytest.raises(IntegrityError):
        await session.flush()


async def test_dtc_cleared_code_can_return(session):
    v = await car(session)
    kw = {"vehicle_id": v.id, "code": "P0420", "status": DtcStatus.stored, "last_seen": NOW}
    old = DtcEvent(first_seen=NOW - timedelta(days=9), cleared_at=NOW, **kw)
    session.add(old)
    session.add(DtcEvent(first_seen=NOW, **kw))
    await session.flush()


@pytest.mark.parametrize("code", ["P017", "X0171", "p0171"])
async def test_dtc_bad_code_rejected(session, code):
    v = await car(session)
    session.add(
        DtcEvent(vehicle_id=v.id, code=code, status="pending", first_seen=NOW, last_seen=NOW)
    )
    with pytest.raises((IntegrityError, DBAPIError)):
        await session.flush()


async def test_mode06_ids_are_bytes(session):
    v = await car(session)
    kw = {"vehicle_id": v.id, "ts": NOW, "tid": 0x80, "uasid": 1, "value": 5, "min": 0, "max": 9}
    session.add(Mode06Result(mid=0x21, **kw))
    await session.flush()
    session.add(Mode06Result(mid=300, **kw))
    with pytest.raises(IntegrityError):
        await session.flush()


async def test_device_command_defaults(session):
    await make_device(session)
    c = DeviceCommand(device_id="FM1-0001", type=DeviceCommandType.health_scan)
    session.add(c)
    await session.flush()
    await session.refresh(c)
    assert c.status == DeviceCommandStatus.queued and c.payload == {}


# ---------------------------------------------------------------- cascades


async def test_deleting_a_vehicle_cascades_but_not_readings(session):
    """Trips, DTCs, Mode 06 and assignments go with the vehicle. Readings don't (no FK):
    the purge job deletes them explicitly (tested in test_jobs)."""
    v = await car(session)
    await make_device(session)
    session.add(DeviceAssignment(device_id="FM1-0001", vehicle_id=v.id))
    session.add(Trip(vehicle_id=v.id, device_id="FM1-0001", device_boot=0, device_trip=1))
    session.add(
        DtcEvent(vehicle_id=v.id, code="P0128", status="stored", first_seen=NOW, last_seen=NOW)
    )
    await session.flush()
    await session.delete(v)
    await session.flush()
    for model in (DeviceAssignment, Trip, DtcEvent):
        assert await session.scalar(select(func.count()).select_from(model)) == 0
    assert await session.get(Device, "FM1-0001") is not None  # the device itself stays

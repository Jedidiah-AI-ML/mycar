"""Phase 2 step 8 gate: device ingestion.

- the same batch twice -> second is dup:true and nothing more is stored
- batches shuffled -> identical readings
- bad or expired token -> 401
- unclaimed device -> acked and discarded (Q3)
- no location consent -> zero GNSS rows
- clock-skewed device -> corrected timestamps
- oversize body -> 413; rate limit -> 429
"""

import gzip
import json
import random
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import jsonschema
import pytest
from sqlalchemy import func, select, text, update

from app.api import ingest
from app.gateway import auth
from app.gateway.contract import SCHEMA_FILE
from app.gateway.ratelimit import RateLimiter
from app.gateway.signals import catalogue
from app.models import (
    AuditEvent,
    Device,
    DeviceAssignment,
    DeviceBatch,
    DeviceCommand,
    DeviceCommandStatus,
    DeviceCommandType,
    DtcEvent,
    Job,
    Mode06Result,
    Trip,
    Vehicle,
    positions,
    readings,
)
from tests.helpers import signup
from tests.test_contract import T0, typical_batch, with_events
from tests.test_jobs import fast_worker
from tests.test_vehicles import add_vehicle

NOW_MS = T0 + 61_000  # the server's clock for these tests: just after a typical batch
URL = "/ingest/freematics_one_plus"


@dataclass
class Rig:
    device_id: str
    token: str
    vehicle_id: uuid.UUID
    owner: object

    @property
    def headers(self) -> dict:
        return {"Authorization": f"Bearer {self.token}", "Content-Encoding": "gzip"}


@pytest.fixture(autouse=True)
def _fresh_limits():
    ingest.limiter.reset()
    yield
    ingest.limiter.reset()


@pytest.fixture
def at(client):
    """Pin the server clock: `at(ms)`; defaults to NOW_MS."""

    def set_now(ms: int = NOW_MS):
        client.app.dependency_overrides[ingest.server_now_ms] = lambda: ms

    set_now()
    return set_now


async def make_rig(client, session, *, device_id="FM1-0001", email="owner@x.com",
                   consents=("service_processing",), claimed=True) -> Rig:  # fmt: skip
    owner = await signup(client, email, consents=consents)
    vid = (await add_vehicle(client, owner, vin=None))["id"]
    secret = auth.new_device_secret()
    session.add(
        Device(id=device_id, type="freematics_one_plus", token_hash=auth.hash_secret(secret))
    )
    await session.flush()
    if claimed:
        session.add(DeviceAssignment(device_id=device_id, vehicle_id=uuid.UUID(vid)))
    await session.commit()
    return Rig(device_id, auth.device_token(device_id, secret), uuid.UUID(vid), owner)


def batch(rig: Rig, seq=0, trip=42, events=False, **over) -> dict:
    b = typical_batch()
    if events:
        b = with_events(b)
    b.update(device_id=rig.device_id, seq=seq, trip=trip)
    b.update(over)
    return b


async def post(client, rig: Rig, doc: dict, kind="batches", headers=None):
    body = gzip.compress(json.dumps(doc).encode())
    return await client.post(f"{URL}/{kind}", content=body, headers=headers or rig.headers)


async def count(session, table, *where) -> int:
    stmt = select(func.count()).select_from(table)
    for w in where:
        stmt = stmt.where(w)
    return await session.scalar(stmt)


def n_samples(doc: dict) -> int:
    return sum(len(rows) for rows in doc["s"].values())


# ---------------------------------------------------------------- happy path


async def test_batch_is_stored_and_acked(client, session, at):
    rig = await make_rig(client, session)
    doc = batch(rig, events=True)
    r = await post(client, rig, doc)
    assert r.status_code == 200, r.text
    ack = r.json()
    assert ack == {"ok": True, "dup": False, "server_time": NOW_MS, "cmds": []}
    jsonschema.validate(ack, {**json.loads(SCHEMA_FILE.read_text("utf-8")),
                              "oneOf": [{"$ref": "#/$defs/AckV1"}]})  # fmt: skip

    crank = 5
    assert await count(session, readings) == n_samples(doc) + crank
    assert await count(session, positions) == 0  # owner gave no location consent
    trip = await session.scalar(select(Trip))
    assert (trip.device_boot, trip.device_trip) == (17, 42)
    assert trip.started_at == datetime.fromtimestamp(T0 / 1000, UTC)
    assert trip.ended_at == datetime.fromtimestamp((T0 + 59_000) / 1000, UTC)
    assert {e["type"] for e in trip.events} == {"readiness", "harsh", "trip_end"}
    dtc = await session.scalar(select(DtcEvent))
    assert (dtc.code, dtc.status.value, dtc.freeze_frame) == (
        "P0128",
        "pending",
        {"coolant": 71, "rpm": 812},
    )
    assert await count(session, Mode06Result) == 1
    v = await session.get(Vehicle, rig.vehicle_id)
    assert v.vin == "JTDBU4EE9A9123456" and v.vin_source.value == "device"
    assert v.protocol.startswith("ISO 15765") and v.supported_pids == {"mode01": "BE3FA813A005B011"}
    d = await session.get(Device, rig.device_id)
    assert d.last_seen_at == datetime.fromtimestamp(NOW_MS / 1000, UTC)
    assert d.last_rssi == -71 and d.firmware_version == "0.1.0" and d.bytes_month > 0
    key = await session.scalar(select(DeviceBatch))
    assert key.trip_id == trip.id and key.n_samples == n_samples(doc) + crank
    (job,) = (await session.scalars(select(Job).where(Job.name == "trip.assemble"))).all()
    assert job.payload == {"trip_id": str(trip.id)} and job.dedupe_key.endswith(str(trip.id))


async def test_readings_carry_catalogue_ids_and_values(client, session, at):
    rig = await make_rig(client, session)
    doc = batch(rig)
    await post(client, rig, doc)
    cat = catalogue().by_name
    rows = (await session.execute(select(readings.c.ts, readings.c.signal_id, readings.c.value)
                                  .where(readings.c.signal_id == cat["rpm"].id))).all()  # fmt: skip
    want = {(T0 + dt, v) for dt, v in doc["s"]["rpm"]}
    got = {(int(ts.timestamp() * 1000), v) for ts, _, v in rows}
    assert got == want


# ---------------------------------------------------------------- idempotency + order


async def test_same_batch_twice_is_a_duplicate(client, session, at):
    rig = await make_rig(client, session)
    doc = batch(rig, events=True)
    assert (await post(client, rig, doc)).json()["dup"] is False
    before = [await count(session, t) for t in (readings, DeviceBatch, DtcEvent, Mode06Result)]
    r = await post(client, rig, {**doc, "sent_at": doc["sent_at"] + 30_000})  # a later retry
    assert r.status_code == 200 and r.json()["dup"] is True
    after = [await count(session, t) for t in (readings, DeviceBatch, DtcEvent, Mode06Result)]
    assert after == before
    assert await count(session, Job, Job.name == "trip.assemble") == 1


async def test_shuffled_batches_give_identical_readings(client, session, at):
    a = await make_rig(client, session, device_id="FM1-0001", email="a@x.com")
    b = await make_rig(client, session, device_id="FM1-0002", email="b@x.com")
    docs = []
    for seq in range(8):
        d = typical_batch(seed=seq)
        # All delivered after the trip: sent_at is the (correct) device time of sending.
        d.update(seq=seq, t0=T0 + seq * 60_000, sent_at=T0 + 8 * 60_000 + 750)
        docs.append(d)
    at(T0 + 8 * 60_000 + 1000)
    shuffled = docs[:]
    random.Random(4).shuffle(shuffled)
    for d in docs:
        await post(client, a, {**d, "device_id": a.device_id})
    for d in shuffled + shuffled[:3]:  # out of order, with some retries
        await post(client, b, {**d, "device_id": b.device_id})

    async def rows(vid):
        res = await session.execute(
            select(readings.c.ts, readings.c.signal_id, readings.c.value)
            .where(readings.c.vehicle_id == vid)
        )  # fmt: skip
        return sorted(res.all())

    ra, rb = await rows(a.vehicle_id), await rows(b.vehicle_id)
    assert ra == rb and len(ra) == sum(n_samples(d) for d in docs)
    assert ra[0][0] == datetime.fromtimestamp(T0 / 1000, UTC)  # true times, not shifted
    assert await count(session, Trip) == 2  # one trip per device, however batches arrive


# ---------------------------------------------------------------- auth, limits


async def test_bad_tokens_are_401(client, session, at):
    rig = await make_rig(client, session)
    doc = batch(rig)
    did, _ = auth.parse_token(rig.token)
    bad = {
        "missing": {"Content-Encoding": "gzip"},
        "wrong secret": {"Authorization": f"Bearer {did}.{auth.new_device_secret()}"},
        "unknown device": {"Authorization": f"Bearer FM1-9999.{auth.new_device_secret()}"},
        "malformed": {"Authorization": "Bearer not-a-token"},
    }
    for name, h in bad.items():
        r = await post(client, rig, doc, headers={"Content-Encoding": "gzip", **h})
        assert r.status_code == 401, name
    await session.execute(update(Device).values(disabled_at=datetime.now(UTC)))  # lost/retired
    await session.commit()
    assert (await post(client, rig, doc)).status_code == 401
    r = await client.post("/ingest/teltonika/batches", content=b"{}", headers=rig.headers)
    assert r.status_code == 404
    assert await count(session, readings) == 0


async def test_body_limits(client, session, at):
    rig = await make_rig(client, session)
    huge = random.Random(1).randbytes(300 * 1024)  # incompressible, over 256 KB
    r = await client.post(f"{URL}/batches", content=huge, headers=rig.headers)
    assert r.status_code == 413
    bomb = gzip.compress(b"{" + b" " * (3 * 1024 * 1024) + b"}")  # tiny gzip, 3 MB inside
    assert len(bomb) < 10_000
    r = await client.post(f"{URL}/batches", content=bomb, headers=rig.headers)
    assert r.status_code == 413
    r = await client.post(f"{URL}/batches", content=b"not gzip", headers=rig.headers)
    assert r.status_code == 400
    plain = {"Authorization": rig.headers["Authorization"]}  # uncompressed JSON is fine too
    r = await client.post(f"{URL}/batches", content=json.dumps(batch(rig)), headers=plain)
    assert r.status_code == 200


async def test_contract_violations_are_422(client, session, at):
    rig = await make_rig(client, session)
    for doc in (
        {**batch(rig), "v": 2},
        {**batch(rig), "surprise": 1},
        {**batch(rig), "device_id": "FM1-0002"},  # someone else's id with my token
    ):
        r = await post(client, rig, doc)
        assert r.status_code == 422, doc.get("v")
    r = await client.post(f"{URL}/batches", content=gzip.compress(b"[1,"), headers=rig.headers)
    assert r.status_code == 422


async def test_rate_limit_is_429(client, session, at, monkeypatch):
    rig = await make_rig(client, session)
    monkeypatch.setattr(ingest, "limiter", RateLimiter(3))
    codes = [(await post(client, rig, batch(rig, seq=i))).status_code for i in range(4)]
    assert codes == [200, 200, 200, 429]
    r = await post(client, rig, batch(rig, seq=9))
    assert r.status_code == 429 and int(r.headers["Retry-After"]) >= 1


def test_token_bucket_refills():
    t = [0.0]
    lim = RateLimiter(60, clock=lambda: t[0])
    assert all(lim.take("d") == 0 for _ in range(60))
    assert lim.take("d") == pytest.approx(1.0)
    t[0] += 1.0
    assert lim.take("d") == 0 and lim.take("other") == 0


# ---------------------------------------------------------------- privacy, unclaimed


async def test_unclaimed_device_is_acked_and_discarded(client, session, at):
    rig = await make_rig(client, session, claimed=False)
    r = await post(client, rig, batch(rig, events=True))
    assert r.status_code == 200 and r.json()["ok"] is True and r.json()["dup"] is False
    for t in (readings, positions, DeviceBatch, Trip, DtcEvent, Mode06Result):
        assert await count(session, t) == 0
    d = await session.get(Device, rig.device_id)
    assert d.last_seen_at is not None  # still shows as online to the admin


async def test_positions_only_with_location_consent(client, session, at):
    no = await make_rig(client, session, device_id="FM1-0001", email="no@x.com")
    yes = await make_rig(client, session, device_id="FM1-0002", email="yes@x.com",
                         consents=("service_processing", "location"))  # fmt: skip
    doc = batch(no)
    await post(client, no, doc)
    await post(client, yes, {**doc, "device_id": yes.device_id})
    assert await count(session, positions, positions.c.vehicle_id == no.vehicle_id) == 0
    n = await count(session, positions, positions.c.vehicle_id == yes.vehicle_id)
    assert n == len(doc["g"]) > 0


# ---------------------------------------------------------------- clocks


async def test_clock_skew_is_corrected(client, session, at):
    rig = await make_rig(client, session)
    rpm = catalogue().by_name["rpm"].id

    async def first_rpm_ms(trip):
        trip_id = select(Trip.id).where(Trip.device_trip == trip).scalar_subquery()
        ts = await session.scalar(
            select(func.min(readings.c.ts)).where(
                readings.c.signal_id == rpm, readings.c.trip_id == trip_id
            )
        )
        return int(ts.timestamp() * 1000)

    # 1. Clock restarted at 1970 after a reboot: shifted by decades to the right time.
    await post(client, rig, batch(rig, trip=1, t0=2_000, sent_at=62_250))
    assert abs(await first_rpm_ms(1) - (NOW_MS - 60_250)) < 1000
    # 2. GNSS time present: trusted over the server clock (no network latency in it).
    t0 = T0 - 600_000
    await post(client, rig, batch(rig, trip=2, t0=t0, sent_at=t0 + 60_250, gnss_time=T0 + 60_250))
    assert await first_rpm_ms(2) == T0
    # 3. Within 2 s (SEED P8): network latency, left alone.
    at(T0 + 60_250 + 1_500)
    await post(client, rig, batch(rig, trip=3))
    assert await first_rpm_ms(3) == T0
    offsets = (
        await session.scalars(select(DeviceBatch.clock_offset_ms).order_by(DeviceBatch.trip))
    ).all()
    assert offsets[0] > 10**12 and offsets[1] == 600_000 and offsets[2] == 0


# ---------------------------------------------------------------- DTC lists


async def test_dtc_lists_set_and_clear_codes_and_ignore_stale_reports(client, session, at):
    rig = await make_rig(client, session)

    t1, t2, t3 = T0, T0 + 120_000, T0 + 240_000

    def dtc_batch(seq, t0, pending):
        # Sent late, as a device does after an outage: `sent_at` is the time of sending.
        return batch(rig, seq=seq, t0=t0, sent_at=t3 + 60_750,
                     ev=[{"t": 0, "type": "dtc", "mode": "07", "codes": pending}])  # fmt: skip

    at(t3 + 61_000)
    await post(client, rig, dtc_batch(0, t1, ["P0171", "P0128"]))
    await post(client, rig, dtc_batch(2, t3, ["P0171"]))  # P0128 gone at t3
    await post(client, rig, dtc_batch(1, t2, ["P0171", "P0128"]))  # late, older report
    rows = {r.code: r for r in (await session.scalars(select(DtcEvent))).all()}
    assert rows["P0171"].cleared_at is None
    assert rows["P0171"].first_seen == datetime.fromtimestamp(t1 / 1000, UTC)
    assert rows["P0171"].last_seen == datetime.fromtimestamp(t3 / 1000, UTC)
    assert rows["P0128"].cleared_at == datetime.fromtimestamp(t3 / 1000, UTC)  # stays cleared


# ---------------------------------------------------------------- commands


async def test_commands_ride_on_the_ack_and_results_close_them(client, session, at):
    rig = await make_rig(client, session)
    session.add(DeviceCommand(device_id=rig.device_id, type=DeviceCommandType.health_scan))
    await session.commit()
    ack = (await post(client, rig, batch(rig, seq=0))).json()
    (cmd,) = ack["cmds"]
    assert cmd["type"] == "health_scan"
    assert (await post(client, rig, batch(rig, seq=1))).json()["cmds"] == []  # sent, waiting
    # The ack was lost: after command_resend_s the command goes out again.
    await session.execute(
        update(DeviceCommand).values(sent_at=datetime.now(UTC) - timedelta(minutes=5))
    )
    await session.commit()
    assert [c["id"] for c in (await post(client, rig, batch(rig, seq=2))).json()["cmds"]] == [
        cmd["id"]
    ]
    result = {"t": 0, "type": "cmd_result", "command_id": cmd["id"], "status": "done"}
    await post(client, rig, batch(rig, seq=3, ev=[result]))
    session.expire_all()
    c = await session.get(DeviceCommand, cmd["id"])
    assert c.status == DeviceCommandStatus.done and c.result["status"] == "done"


# ---------------------------------------------------------------- critical packets


async def test_critical_packet_stored_and_queued_at_priority_0(client, session, at):
    rig = await make_rig(client, session)
    crit = {"v": 1, "device_id": rig.device_id, "boot": 17, "trip": 42, "t": NOW_MS - 500,
            "sent_at": NOW_MS - 400, "kind": "coolant_high",
            "values": {"coolant": 118, "rpm": 820, "speed": 0, "batt_v": 13.9},
            "pos": [6.52, 3.38]}  # fmt: skip
    r = await post(client, rig, crit, kind="critical")
    assert r.status_code == 200 and r.json()["dup"] is False
    assert await count(session, readings) == 4
    assert await count(session, positions) == 0  # no location consent
    job = await session.scalar(select(Job).where(Job.name == "alert.critical"))
    assert job.priority == 0 and job.payload["kind"] == "coolant_high"
    r = await post(client, rig, crit, kind="critical")  # retried after a lost ack
    assert r.json()["dup"] is True and await count(session, readings) == 4

    assert await fast_worker("w").run_until_empty() >= 1
    actions = (await session.scalars(select(AuditEvent.action))).all()
    assert "device.critical" in actions
    # The batch carrying the same trip later finds the same trip row.
    await post(client, rig, batch(rig))
    assert await count(session, Trip) == 1
    await session.execute(text("SELECT 1"))

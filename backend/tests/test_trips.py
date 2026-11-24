"""Phase 2 step 9 gate: trip assembly.

- the assembled trip is identical regardless of batch arrival order
- a late batch after `complete` re-assembles and changes only what it should
- a trip without a trip_end event (device lost power) closes after 15 min (SEED P11)
"""

import random
import time

from sqlalchemy import select, text, update

from app.jobs.handlers.trips import find_gaps
from app.jobs.pipeline import STAGES, next_stage
from app.models import DeviceBatch, Job, JobStatus, Trip, TripStatus
from tests.test_contract import typical_batch
from tests.test_ingest import at, make_rig, n_samples, post  # noqa: F401 - `at` is a fixture
from tests.test_jobs import fast_worker

# Times near the real clock, so the 15-minute timeout behaves as it would live.
REAL_NOW = int(time.time() * 1000)
T0 = REAL_NOW - 10 * 60_000  # the trip started 10 min ago
SENT = REAL_NOW - 250  # every batch delivered just now (device clock correct)


def trip_batches(rig, n=6, trip=42, end=True) -> list[dict]:
    docs = []
    for seq in range(n):
        d = typical_batch(seed=seq)
        d.update(device_id=rig.device_id, trip=trip, seq=seq, t0=T0 + seq * 60_000, sent_at=SENT)
        if seq == 0:
            d["ev"] = [{"t": 0, "type": "trip_start"}]
        if seq == n - 1 and end:
            d["ev"] = [{"t": 59_500, "type": "trip_end", "reason": "ignition_off"}]
            d["final"] = True
        docs.append(d)
    return docs


async def assemble(session) -> int:
    """Run every trip.assemble job that is due now (skipping the 60 s debounce)."""
    await session.execute(
        update(Job)
        .where(Job.name == "trip.assemble", Job.status == JobStatus.queued)
        .where(~Job.dedupe_key.like("trip.close:%"))
        .values(run_at=text("now()"))
    )
    await session.commit()
    n = await fast_worker("w").run_until_empty()
    session.expire_all()
    return n


def summary(t: Trip) -> tuple:
    return (t.started_at, t.ended_at, t.end_reason, t.n_samples, t.signal_counts, t.gaps,
            round(t.completeness, 6), t.status)  # fmt: skip


# ---------------------------------------------------------------- order independence


async def test_assembled_trip_is_identical_in_any_arrival_order(client, session, at):  # noqa: F811
    at(REAL_NOW)
    a = await make_rig(client, session, device_id="FM1-0001", email="a@x.com")
    b = await make_rig(client, session, device_id="FM1-0002", email="b@x.com")
    docs = trip_batches(a)
    for d in docs:
        await post(client, a, d)
    shuffled = docs[:]
    random.Random(7).shuffle(shuffled)
    for d in shuffled + shuffled[:2]:  # out of order, plus retried duplicates
        await post(client, b, {**d, "device_id": b.device_id})
    await assemble(session)

    ta = await session.scalar(select(Trip).where(Trip.device_id == a.device_id))
    tb = await session.scalar(select(Trip).where(Trip.device_id == b.device_id))
    assert summary(ta) == summary(tb)
    assert ta.status == TripStatus.complete and ta.end_reason == "ignition_off"
    assert ta.gaps == [] and ta.completeness > 0.99
    assert ta.n_samples == sum(n_samples(d) for d in docs)
    assert ta.signal_counts["rpm"] == 6 * 60
    assert ta.started_at.timestamp() * 1000 == T0
    assert ta.ended_at.timestamp() * 1000 >= T0 + 5 * 60_000 + 59_000


# ---------------------------------------------------------------- late batch


async def test_late_batch_reassembles_and_changes_only_what_it_should(client, session, at):  # noqa: F811
    at(REAL_NOW)
    rig = await make_rig(client, session)
    docs = trip_batches(rig)
    for d in docs[:3] + docs[4:]:  # batch 3 is stuck in the device's flash (outage)
        await post(client, rig, d)
    await assemble(session)
    before = await session.scalar(select(Trip))
    b = summary(before)
    assert before.status == TripStatus.complete
    (gap,) = before.gaps  # the hole where batch 3 belongs: about 61 s
    assert 60_000 < gap[1] - gap[0] < 63_000
    assert before.completeness < 0.85

    await post(client, rig, docs[3])  # coverage is back: the late batch arrives
    assert await assemble(session) >= 1
    after = await session.scalar(select(Trip))
    assert after.status == TripStatus.late_data
    assert after.gaps == [] and after.completeness > 0.99
    # `before` is the same ORM object, now refreshed: compare with the snapshot `b`.
    assert after.n_samples == b[3] + n_samples(docs[3])
    assert (after.started_at, after.ended_at, after.end_reason) == b[:3]  # unchanged
    for name, c in b[4].items():
        assert after.signal_counts[name] >= c

    # Assembling again with nothing new changes nothing.
    await session.execute(
        update(Job).where(Job.name == "trip.assemble").values(status=JobStatus.succeeded)
    )
    from app.jobs.queue import enqueue

    await enqueue(session, "trip.assemble", {"trip_id": str(after.id)})
    await session.commit()
    await assemble(session)
    again = await session.scalar(select(Trip))
    assert summary(again) == summary(after)


# ---------------------------------------------------------------- lost power


async def test_trip_without_trip_end_closes_after_timeout(client, session, at):  # noqa: F811
    at(REAL_NOW)
    rig = await make_rig(client, session)
    for d in trip_batches(rig, n=4, end=False):
        await post(client, rig, d)
    await assemble(session)
    trip = await session.scalar(select(Trip))
    assert trip.status == TripStatus.open and trip.ended_at is None
    # It scheduled its own check for when the trip would time out.
    check = await session.scalar(select(Job).where(Job.dedupe_key.like("trip.close:%")))
    assert check is not None and check.status == JobStatus.queued
    due_in = (check.run_at - trip.assembled_through).total_seconds()
    assert 900 <= due_in <= 902

    # 15 quiet minutes pass (the device lost power): the check runs and closes the trip.
    await session.execute(
        update(DeviceBatch).values(received_at=DeviceBatch.received_at - text("interval '20 min'"))
    )
    await session.execute(update(Job).where(Job.id == check.id).values(run_at=text("now()")))
    await session.commit()
    await fast_worker("w").run_until_empty()
    session.expire_all()
    trip = await session.scalar(select(Trip))
    assert trip.status == TripStatus.complete and trip.end_reason == "timeout"
    last_reading_ms = T0 + 3 * 60_000 + 59_000
    assert abs(trip.ended_at.timestamp() * 1000 - last_reading_ms) < 2000


async def test_open_trip_still_receiving_data_stays_open(client, session, at):  # noqa: F811
    at(REAL_NOW)
    rig = await make_rig(client, session)
    for d in trip_batches(rig, n=2, end=False):
        await post(client, rig, d)
    await assemble(session)
    # The close check comes due, but a batch arrived 5 min ago: not quiet long enough.
    await session.execute(
        update(DeviceBatch).values(received_at=DeviceBatch.received_at - text("interval '5 min'"))
    )
    await session.execute(
        update(Job).where(Job.dedupe_key.like("trip.close:%")).values(run_at=text("now()"))
    )
    await session.commit()
    await fast_worker("w").run_until_empty()
    session.expire_all()
    trip = await session.scalar(select(Trip))
    assert trip.status == TripStatus.open
    # ...and it re-armed itself for the new due time.
    pending = (
        await session.scalars(
            select(Job).where(Job.dedupe_key.like("trip.close:%"), Job.status == JobStatus.queued)
        )
    ).all()
    assert len(pending) == 1


# ---------------------------------------------------------------- pieces


def test_find_gaps():
    ts = [0, 1000, 2000, 15_000, 16_000, 40_000]
    assert find_gaps(ts, 10) == [[2000, 15_000], [16_000, 40_000]]
    assert find_gaps([], 10) == [] and find_gaps([5], 10) == []


def test_pipeline_stages_through_phase_3():
    assert STAGES == ("trip.assemble", "trip.summarise", "dtc.interpret")
    assert next_stage("trip.assemble") == "trip.summarise"
    assert next_stage("trip.summarise") == "dtc.interpret"
    assert next_stage("dtc.interpret") is None


# ---------------------------------------------------------------- concurrency


async def test_batch_during_running_assembly_gets_its_own_assembly(client, session, at):  # noqa: F811
    """While trip.assemble runs it holds the trip row and its dedupe key. A batch arriving
    then must wait for the lock and queue a fresh assembly, not be silently skipped."""
    import asyncio

    from app.db.session import get_sessionmaker

    at(REAL_NOW)
    rig = await make_rig(client, session)
    docs = trip_batches(rig, n=3)
    await post(client, rig, docs[0])
    trip = await session.scalar(select(Trip))
    job = await session.scalar(select(Job).where(Job.name == "trip.assemble"))
    await session.commit()

    async with get_sessionmaker()() as running:  # plays the worker, mid-job
        await running.execute(update(Job).where(Job.id == job.id).values(status=JobStatus.running))
        await running.execute(select(Trip).where(Trip.id == trip.id).with_for_update())
        await running.commit()  # job visibly running...
        await running.execute(select(Trip).where(Trip.id == trip.id).with_for_update())
        task = asyncio.create_task(post(client, rig, docs[1]))  # ...and holding the trip
        await asyncio.sleep(0.5)
        assert not task.done()  # the batch waits for the running assembly
        await running.execute(
            update(Job).where(Job.id == job.id).values(status=JobStatus.succeeded)
        )
        await running.commit()
    r = await asyncio.wait_for(task, 10)
    assert r.status_code == 200 and r.json()["dup"] is False
    queued = (
        await session.scalars(
            select(Job).where(Job.name == "trip.assemble", Job.status == JobStatus.queued)
        )
    ).all()
    assert len(queued) == 1  # the new batch will be assembled

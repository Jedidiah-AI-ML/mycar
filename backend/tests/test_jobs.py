import asyncio
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select, text, update

from app.db.session import get_sessionmaker
from app.jobs.queue import enqueue
from app.jobs.registry import job
from app.jobs.worker import Worker
from app.models import AuditEvent, Job, JobStatus, User, Vehicle, VehicleAccess
from app.services import audit
from tests.helpers import signup
from tests.test_vehicles import add_vehicle

# ---------------------------------------------------------------- test handlers


@job("test.record")
async def _record(payload: dict, session) -> None:
    # Side effect we can count: one audit row per run.
    await audit.record(session, None, "test.ran", "test", payload["n"])
    await asyncio.sleep(0)  # yield, so concurrent workers really interleave


@job("test.boom", max_attempts=3)
async def _boom(payload: dict, session) -> None:
    await audit.record(session, None, "test.should_roll_back", "test", "x")
    raise RuntimeError("boom")


def fast_worker(name: str) -> Worker:
    return Worker(worker_id=name, backoff_base_s=0, lease_s=60)


async def _jobs(session) -> list[Job]:
    return list((await session.scalars(select(Job).order_by(Job.id))).all())


# ---------------------------------------------------------------- tests


async def test_two_workers_run_each_job_exactly_once(session):
    for n in range(200):
        await enqueue(session, "test.record", {"n": n})
    await session.commit()

    done = await asyncio.gather(
        fast_worker("w1").run_until_empty(), fast_worker("w2").run_until_empty()
    )
    assert sum(done) == 200
    assert all(d > 0 for d in done), "both workers should have taken work"

    per_n = (
        await session.execute(
            select(AuditEvent.subject_id, func.count())
            .where(AuditEvent.action == "test.ran")
            .group_by(AuditEvent.subject_id)
        )
    ).all()
    assert len(per_n) == 200 and all(c == 1 for _, c in per_n)
    assert {j.status for j in await _jobs(session)} == {JobStatus.succeeded}


async def test_failing_job_retries_then_dies_and_rolls_back(session):
    await enqueue(session, "test.boom")
    await session.commit()
    w = fast_worker("w")
    assert await w.run_until_empty() == 3  # max_attempts from the decorator
    (j,) = await _jobs(session)
    assert j.status == JobStatus.dead and j.attempts == 3
    assert "boom" in j.last_error
    # The handler's own writes were rolled back on every attempt.
    n = await session.scalar(
        select(func.count()).where(AuditEvent.action == "test.should_roll_back")
    )
    assert n == 0


async def test_backoff_delays_retry(session):
    await enqueue(session, "test.boom")
    await session.commit()
    w = Worker(worker_id="w", backoff_base_s=3600)
    assert await w.run_until_empty() == 1  # second attempt is an hour away
    (j,) = await _jobs(session)
    assert j.status == JobStatus.failed
    assert j.run_at > datetime.now(UTC) + timedelta(minutes=59)


async def test_enqueue_rolls_back_with_transaction(session):
    await enqueue(session, "test.record", {"n": 1})
    await session.rollback()
    assert await _jobs(session) == []


async def test_dedupe_key(session):
    a = await enqueue(session, "test.record", {"n": 1}, dedupe_key="k")
    b = await enqueue(session, "test.record", {"n": 2}, dedupe_key="k")
    await session.commit()
    assert a is not None and b is None
    await fast_worker("w").run_until_empty()
    # Once finished, the same key can be queued again.
    assert await enqueue(session, "test.record", {"n": 3}, dedupe_key="k") is not None


async def test_expired_lease_is_reclaimed(session):
    await enqueue(session, "test.record", {"n": 7})
    await session.commit()
    # Simulate a worker that claimed the job and then crashed.
    await session.execute(
        update(Job).values(
            status=JobStatus.running,
            attempts=1,
            locked_by="dead-worker",
            locked_until=datetime.now(UTC) - timedelta(seconds=1),
        )
    )
    await session.commit()
    w = fast_worker("w")
    assert await w.run_until_empty() == 0  # running jobs aren't claimable...
    assert await w.reclaim_expired() == 1  # ...until their lease is reclaimed
    assert await w.run_until_empty() == 1
    (j,) = await _jobs(session)
    assert j.status == JobStatus.succeeded and j.attempts == 2


async def test_future_jobs_wait(session):
    await enqueue(session, "test.record", {"n": 1}, run_at=datetime.now(UTC) + timedelta(hours=1))
    await session.commit()
    assert await fast_worker("w").run_until_empty() == 0


async def test_unknown_job_goes_dead(session):
    await enqueue(session, "no.such.job", max_attempts=5)
    await session.commit()
    assert await fast_worker("w").run_until_empty() == 1
    (j,) = await _jobs(session)
    assert j.status == JobStatus.dead and "no handler" in j.last_error


async def test_notify_wakes_the_long_running_worker(session):
    w = Worker(worker_id="w", poll_interval_s=30)  # polling alone would take 30 s
    task = asyncio.create_task(w.run())
    await asyncio.sleep(0.5)  # let it LISTEN
    await enqueue(session, "test.record", {"n": 1})
    await session.commit()
    for _ in range(50):
        await asyncio.sleep(0.1)
        if (await _jobs(session))[0].status == JobStatus.succeeded:
            break
        session.expire_all()
    w.stop()
    await asyncio.wait_for(task, 5)
    assert (await _jobs(session))[0].status == JobStatus.succeeded


# ---------------------------------------------------------------- account purge


async def test_delete_me_schedules_purge_and_purge_removes_data(client, session):
    owner = await signup(client, "owner@x.com")
    viewer = await signup(client, "viewer@x.com")
    vid = (await add_vehicle(client, owner))["id"]
    await client.post(
        f"/vehicles/{vid}/access",
        json={"email": viewer.email, "role": "viewer"},
        headers=owner.headers,
    )
    assert (await client.delete("/me", headers=owner.headers)).status_code == 204

    (j,) = await _jobs(session)
    assert j.name == "account.purge"
    assert j.run_at > datetime.now(UTC) + timedelta(days=29)

    # Fast-forward the grace period and run the worker.
    await session.execute(update(Job).values(run_at=func.now()))
    await session.commit()
    assert await fast_worker("w").run_until_empty() == 1

    session.expire_all()
    assert await session.scalar(select(func.count()).select_from(User)) == 1  # viewer only
    assert await session.scalar(select(func.count()).select_from(Vehicle)) == 0
    assert await session.scalar(select(func.count()).select_from(VehicleAccess)) == 0
    # Viewer is untouched.
    assert (await client.get("/me", headers=viewer.headers)).status_code == 200


async def test_admin_ping_endpoint(client, session):
    u = await signup(client)
    assert (await client.post("/admin/jobs/ping", headers=u.headers)).status_code == 404
    await session.execute(text("UPDATE users SET is_admin = true"))
    await session.commit()
    r = await client.post("/admin/jobs/ping", headers=u.headers)
    assert r.status_code == 202
    jid = r.json()["id"]
    await Worker(get_sessionmaker(), worker_id="w").run_until_empty()
    r = await client.get(f"/admin/jobs/{jid}", headers=u.headers)
    assert r.json()["status"] == "succeeded"

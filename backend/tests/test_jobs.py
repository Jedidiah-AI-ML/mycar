import asyncio
from datetime import UTC, datetime, timedelta

from sqlalchemy import event, func, select, text, update

from app.core.config import get_settings
from app.db.session import IdlePoolReleaser, get_engine, get_sessionmaker, make_engine
from app.jobs import wake
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


async def _wait_succeeded(session, limit_s: float = 5.0) -> float:
    """Polls (from the test, not the worker) until the first job succeeds; returns seconds."""
    loop = asyncio.get_running_loop()
    start = loop.time()
    while loop.time() - start < limit_s:
        session.expire_all()
        jobs = await _jobs(session)
        if jobs and jobs[0].status == JobStatus.succeeded:
            return loop.time() - start
        await asyncio.sleep(0.05)
    raise AssertionError("job did not succeed in time")


class _QueryCounter:
    def __init__(self):
        self.n = 0
        self.engine = get_engine().sync_engine
        event.listen(self.engine, "before_cursor_execute", self._count)

    def _count(self, *_):
        self.n += 1

    def close(self):
        event.remove(self.engine, "before_cursor_execute", self._count)


async def test_in_process_wake_after_commit(session):
    w = Worker(worker_id="w", max_idle_s=60)  # without a wake it would sleep a minute
    wake.install(w._wake)
    task = asyncio.create_task(w.run())
    try:
        await asyncio.sleep(0.3)  # let it go idle
        await enqueue(session, "test.record", {"n": 1})
        assert not w._wake.is_set()  # nothing visible to the worker before commit
        await session.commit()
        assert await _wait_succeeded(session) < 2
    finally:
        w.stop()
        wake.install(None)
        await asyncio.wait_for(task, 5)


async def test_rolled_back_enqueue_does_not_wake(session):
    ev = asyncio.Event()
    wake.install(ev)
    try:
        await enqueue(session, "test.record", {"n": 1})
        await session.rollback()
        assert not ev.is_set()
    finally:
        wake.install(None)


async def test_idle_worker_sends_no_queries(session):
    """The point of ADR-008: an idle worker must leave the database alone."""
    w = Worker(worker_id="w", max_idle_s=60)
    task = asyncio.create_task(w.run())
    await asyncio.sleep(0.5)  # startup: reclaim + empty queue check + next-due query
    counter = _QueryCounter()
    try:
        await asyncio.sleep(2.0)
        assert counter.n == 0
    finally:
        counter.close()
        w.stop()
        await asyncio.wait_for(task, 5)


async def test_idle_worker_wakes_for_scheduled_job(session):
    await enqueue(
        session, "test.record", {"n": 1}, run_at=datetime.now(UTC) + timedelta(seconds=1.5)
    )
    await session.commit()
    w = Worker(worker_id="w", max_idle_s=60)  # no wake installed: it must use the due time
    task = asyncio.create_task(w.run())
    try:
        took = await _wait_succeeded(session, limit_s=6)
        assert 1.0 < took < 4.0
    finally:
        w.stop()
        await asyncio.wait_for(task, 5)


async def test_listen_wakes_standalone_worker(session):
    w = Worker(worker_id="w", max_idle_s=60, listen=True)
    task = asyncio.create_task(w.run())
    try:
        await asyncio.sleep(0.5)  # let it LISTEN
        await enqueue(session, "test.record", {"n": 1})
        await session.commit()
        assert await _wait_succeeded(session) < 2
    finally:
        w.stop()
        await asyncio.wait_for(task, 5)


async def test_idle_pool_is_released():
    engine = make_engine(get_settings().database_url, pool_size=2)
    releaser = IdlePoolReleaser(engine, idle_s=0.2)
    try:
        async with engine.connect() as c:
            await c.execute(text("SELECT 1"))
        assert engine.pool.checkedin() == 1
        assert await releaser.maybe_release() is False  # used just now
        await asyncio.sleep(0.3)
        assert await releaser.maybe_release() is True
        assert engine.pool.checkedin() == 0
        # Still works afterwards, and the listener survived the new pool.
        async with engine.connect() as c:
            await c.execute(text("SELECT 1"))
        assert await releaser.maybe_release() is False
    finally:
        await engine.dispose()


async def test_embedded_worker_runs_inside_the_api(session):
    from app.main import create_app

    settings = get_settings()
    settings.worker_mode = "embedded"
    try:
        app = create_app()
        async with app.router.lifespan_context(app):
            await enqueue(session, "test.record", {"n": 1})
            await session.commit()
            assert await _wait_succeeded(session) < 2
    finally:
        settings.worker_mode = "off"


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

"""Background worker:  python -m app.jobs.worker

How it picks work safely when several workers run at once:
  `SELECT ... FOR UPDATE SKIP LOCKED` locks the next ready job row; any row another
  worker has already locked is skipped instead of waited on, so no job is taken twice.
The claimed job gets a *lease* (`locked_until`). If a worker dies mid-job, the lease
expires and another worker's reclaim pass puts the job back in the queue.
Workers sleep until Postgres `NOTIFY`s them about a new job, with a polling fallback.
"""

import asyncio
import logging
import os
import signal
import socket
import uuid
from datetime import UTC, datetime, timedelta

import asyncpg
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import get_settings
from app.core.logging import configure_logging
from app.db.session import get_sessionmaker
from app.jobs.queue import NOTIFY_CHANNEL
from app.jobs.registry import get_job

log = logging.getLogger("app.worker")

_CLAIM = text(
    """
    UPDATE jobs SET status = 'running', locked_by = :worker,
           locked_until = now() + make_interval(secs => :lease), attempts = attempts + 1
    WHERE id = (
        SELECT id FROM jobs
        WHERE status IN ('queued', 'failed') AND run_at <= now()
        ORDER BY priority, run_at, id
        FOR UPDATE SKIP LOCKED
        LIMIT 1
    )
    RETURNING id, name, payload, attempts, max_attempts
    """
)

_RECLAIM = text(
    """
    UPDATE jobs
    SET status = CASE WHEN attempts >= max_attempts THEN 'dead'::job_status
                      ELSE 'failed'::job_status END,
        locked_by = NULL, locked_until = NULL,
        last_error = 'lease expired (worker crashed or job ran too long)',
        finished_at = CASE WHEN attempts >= max_attempts THEN now() END
    WHERE status = 'running' AND locked_until < now()
    RETURNING id
    """
)

_SUCCEED = text(
    "UPDATE jobs SET status = 'succeeded', finished_at = now(), locked_by = NULL, "
    "locked_until = NULL, last_error = NULL WHERE id = :id"
)

_FAIL = text(
    """
    UPDATE jobs
    SET status = CASE WHEN :dead THEN 'dead'::job_status ELSE 'failed'::job_status END,
        run_at = :retry_at, last_error = :error, locked_by = NULL, locked_until = NULL,
        finished_at = CASE WHEN :dead THEN now() END
    WHERE id = :id
    """
)


class Worker:
    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession] | None = None,
        *,
        worker_id: str | None = None,
        lease_s: float = 300,
        poll_interval_s: float = 5.0,
        backoff_base_s: float = 10.0,  # SEED - validate (S10): retry n waits base * 2^(n-1)
    ) -> None:
        self.sm = sessionmaker or get_sessionmaker()
        self.worker_id = worker_id or f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:6]}"
        self.lease_s = lease_s
        self.poll_interval_s = poll_interval_s
        self.backoff_base_s = backoff_base_s
        self._wake = asyncio.Event()
        self._stop = asyncio.Event()

    # ------------------------------------------------------------ one step at a time

    async def reclaim_expired(self) -> int:
        async with self.sm() as s:
            ids = (await s.execute(_RECLAIM)).scalars().all()
            await s.commit()
        if ids:
            log.warning("reclaimed expired leases", extra={"job_ids": ids})
        return len(ids)

    async def run_once(self) -> bool:
        """Claim and run one job. Returns False if nothing was ready."""
        async with self.sm() as s:
            row = (
                await s.execute(_CLAIM, {"worker": self.worker_id, "lease": self.lease_s})
            ).first()
            await s.commit()
        if row is None:
            return False

        job_id, name, payload, attempts, max_attempts = row
        ctx = {"job_id": job_id, "job": name, "attempt": attempts}
        spec = get_job(name)
        started = datetime.now(UTC)
        try:
            if spec is None:
                raise LookupError(f"no handler registered for job {name!r}")
            async with self.sm() as s:
                await spec.fn(payload, s)
                await s.execute(_SUCCEED, {"id": job_id})
                await s.commit()  # handler effects + success mark: one transaction
        except Exception as exc:  # noqa: BLE001 - every failure is recorded on the job
            dead = spec is None or attempts >= max_attempts
            delay = self.backoff_base_s * 2 ** (attempts - 1)
            async with self.sm() as s:
                await s.execute(
                    _FAIL,
                    {
                        "id": job_id,
                        "dead": dead,
                        "retry_at": datetime.now(UTC) + timedelta(seconds=delay),
                        "error": f"{type(exc).__name__}: {exc}"[:2000],
                    },
                )
                await s.commit()
            log.exception("job failed" + (" permanently" if dead else ", will retry"), extra=ctx)
        else:
            ms = round((datetime.now(UTC) - started).total_seconds() * 1000, 1)
            log.info("job succeeded", extra=ctx | {"duration_ms": ms})
        return True

    async def run_until_empty(self) -> int:
        """Process jobs until none are ready (used by tests and one-off runs)."""
        n = 0
        while await self.run_once():
            n += 1
        return n

    # ------------------------------------------------------------ long-running loop

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    async def _listen(self) -> asyncpg.Connection | None:
        dsn = get_settings().raw_asyncpg_dsn
        try:
            conn = await asyncpg.connect(dsn)
            await conn.add_listener(NOTIFY_CHANNEL, lambda *_: self._wake.set())
            return conn
        except Exception:  # noqa: BLE001 - polling still works without LISTEN
            log.warning("LISTEN unavailable; falling back to polling", exc_info=True)
            return None

    async def run(self) -> None:
        log.info("worker started", extra={"worker_id": self.worker_id})
        listener = await self._listen()
        last_reclaim = 0.0
        loop = asyncio.get_running_loop()
        try:
            while not self._stop.is_set():
                if loop.time() - last_reclaim > 30:
                    await self.reclaim_expired()
                    last_reclaim = loop.time()
                self._wake.clear()
                try:
                    if await self.run_once():
                        continue
                except Exception:  # noqa: BLE001 - e.g. DB briefly unreachable
                    log.exception("worker loop error")
                try:
                    await asyncio.wait_for(self._wake.wait(), self.poll_interval_s)
                except TimeoutError:
                    pass
        finally:
            if listener is not None:
                await listener.close()
            log.info("worker stopped", extra={"worker_id": self.worker_id})


def main() -> None:
    import app.jobs.handlers  # noqa: F401 - registers every handler

    configure_logging(get_settings().log_level)
    worker = Worker()

    async def _main() -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(sig, worker.stop)  # graceful stop on deploy
            except NotImplementedError:  # Windows dev machines
                pass
        await worker.run()

    asyncio.run(_main())


if __name__ == "__main__":
    main()

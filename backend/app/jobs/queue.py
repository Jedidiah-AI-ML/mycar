"""The only way business code schedules background work (ADR-004).

`enqueue` writes into the caller's session, so the job is committed or rolled back
together with the data it refers to: a job can never point at rows that don't exist.
"""

import json
from datetime import datetime

from sqlalchemy import text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.jobs.registry import get_job
from app.models import Job

NOTIFY_CHANNEL = "autocare_jobs"
_DEDUPE_WHERE = text("dedupe_key IS NOT NULL AND status IN ('queued', 'running', 'failed')")


async def enqueue(
    session: AsyncSession,
    name: str,
    payload: dict | None = None,
    *,
    run_at: datetime | None = None,
    dedupe_key: str | None = None,
    priority: int = 100,
    max_attempts: int | None = None,
) -> int | None:
    """Queue a job. Returns its id, or None if an active job has the same dedupe_key."""
    payload = payload or {}
    json.dumps(payload)  # fail here, not in the worker, if it isn't JSON-serialisable
    spec = get_job(name)
    values: dict = {
        "name": name,
        "payload": payload,
        "priority": priority,
        "dedupe_key": dedupe_key,
        "max_attempts": max_attempts or (spec.max_attempts if spec else 5),
    }
    if run_at is not None:
        values["run_at"] = run_at
    stmt = (
        insert(Job)
        .values(**values)
        .on_conflict_do_nothing(index_elements=["dedupe_key"], index_where=_DEDUPE_WHERE)
        .returning(Job.id)
    )
    job_id = await session.scalar(stmt)
    if job_id is not None:
        # Postgres delivers NOTIFY only when the transaction commits.
        await session.execute(
            text("SELECT pg_notify(:ch, :id)"), {"ch": NOTIFY_CHANNEL, "id": str(job_id)}
        )
    return job_id

"""Admin-only endpoints. `is_admin` is set by hand in the database, never via the API."""

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict

from app.api.deps import CurrentUser, SessionDep
from app.jobs.queue import enqueue
from app.models import Job, JobStatus, User


async def require_admin(user: CurrentUser) -> User:
    if not user.is_admin:
        # 404 rather than 403: don't advertise that admin routes exist.
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found")
    return user


AdminUser = Annotated[User, Depends(require_admin)]
router = APIRouter(prefix="/admin", tags=["admin"])


class JobOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    status: JobStatus
    attempts: int
    last_error: str | None
    created_at: datetime
    finished_at: datetime | None


@router.post("/jobs/ping", response_model=JobOut, status_code=status.HTTP_202_ACCEPTED)
async def enqueue_ping(admin: AdminUser, session: SessionDep):
    job_id = await enqueue(session, "system.ping", {"requested_by": str(admin.id)})
    await session.commit()
    return await session.get(Job, job_id)


@router.get("/jobs/{job_id}", response_model=JobOut)
async def get_job_status(job_id: int, admin: AdminUser, session: SessionDep):
    job = await session.get(Job, job_id)
    if job is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Job not found")
    return job

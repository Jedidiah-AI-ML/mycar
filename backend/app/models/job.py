from datetime import datetime

from sqlalchemy import BigInteger, Identity, Index, SmallInteger, String, Text, func, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.enums import JobStatus, pg_enum


class Job(Base):
    """Postgres-backed job queue (ADR-004). Only `app.jobs` reads or writes this table."""

    __tablename__ = "jobs"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    name: Mapped[str] = mapped_column(String(100))
    payload: Mapped[dict] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    status: Mapped[JobStatus] = mapped_column(
        pg_enum(JobStatus, "job_status"), server_default=JobStatus.queued.value
    )
    priority: Mapped[int] = mapped_column(SmallInteger, server_default=text("100"))  # lower first
    run_at: Mapped[datetime] = mapped_column(server_default=func.now())
    attempts: Mapped[int] = mapped_column(server_default=text("0"))
    max_attempts: Mapped[int] = mapped_column(server_default=text("5"))
    locked_by: Mapped[str | None] = mapped_column(String(100))
    locked_until: Mapped[datetime | None]
    last_error: Mapped[str | None] = mapped_column(Text)
    dedupe_key: Mapped[str | None] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    finished_at: Mapped[datetime | None]

    __table_args__ = (
        # The worker's "next job" query reads only this small partial index.
        Index(
            "ix_jobs_ready",
            "priority",
            "run_at",
            postgresql_where=text("status IN ('queued', 'failed')"),
        ),
        # Expired leases (crashed worker) are found through this one.
        Index("ix_jobs_running_lease", "locked_until", postgresql_where=text("status = 'running'")),
        Index(
            "uq_jobs_dedupe_active",
            "dedupe_key",
            unique=True,
            postgresql_where=text(
                "dedupe_key IS NOT NULL AND status IN ('queued', 'running', 'failed')"
            ),
        ),
    )

"""trips: events (step 8) and assembly results (step 9)

- events: readiness snapshots, harsh driving, trip_end reason, unknown event types
- n_batches, signal_counts, end_reason, assembled_at, assembled_through: written by
  trip.assemble

Written by hand (columns only). See docs/plans/phase2-plan.md steps 8 and 9.

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-19
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    jsonb = postgresql.JSONB(astext_type=sa.Text())
    op.add_column(
        "trips",
        sa.Column("events", jsonb, server_default=sa.text("'[]'::jsonb"), nullable=False),
    )
    op.add_column(
        "trips",
        sa.Column("signal_counts", jsonb, server_default=sa.text("'{}'::jsonb"), nullable=False),
    )
    op.add_column(
        "trips", sa.Column("n_batches", sa.Integer(), server_default=sa.text("0"), nullable=False)
    )
    op.add_column("trips", sa.Column("end_reason", sa.String(length=16), nullable=True))
    op.add_column("trips", sa.Column("assembled_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column(
        "trips", sa.Column("assembled_through", sa.DateTime(timezone=True), nullable=True)
    )


def downgrade() -> None:
    cols = ("assembled_through", "assembled_at", "end_reason", "n_batches", "signal_counts",
            "events")  # fmt: skip
    for col in cols:
        op.drop_column("trips", col)

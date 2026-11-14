import uuid
from datetime import datetime

from sqlalchemy import BigInteger, ForeignKey, Identity, Index, String, func, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.enums import VehicleRole, pg_enum


class VehicleAccess(Base):
    """Who can see a vehicle, and as what. Revoking keeps the row (sharing is auditable)."""

    __tablename__ = "vehicle_access"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, server_default=func.gen_random_uuid())
    vehicle_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("vehicles.id", ondelete="CASCADE"))
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    role: Mapped[VehicleRole] = mapped_column(pg_enum(VehicleRole, "vehicle_role"))
    granted_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    granted_at: Mapped[datetime] = mapped_column(server_default=func.now())
    revoked_at: Mapped[datetime | None]

    __table_args__ = (
        Index(
            "uq_vehicle_access_active",
            "vehicle_id",
            "user_id",
            unique=True,
            postgresql_where=text("revoked_at IS NULL"),
        ),
        # Exactly one active owner per vehicle.
        Index(
            "uq_vehicle_access_one_owner",
            "vehicle_id",
            unique=True,
            postgresql_where=text("role = 'owner' AND revoked_at IS NULL"),
        ),
    )


class AuditEvent(Base):
    __tablename__ = "audit_events"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    actor_user_id: Mapped[uuid.UUID | None]  # no FK: the audit trail must outlive the user row
    action: Mapped[str] = mapped_column(String(64))  # e.g. access.granted, consent.revoked
    subject_type: Mapped[str] = mapped_column(String(32))
    subject_id: Mapped[str] = mapped_column(String(64))
    details: Mapped[dict] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())

    __table_args__ = (Index("ix_audit_events_subject", "subject_type", "subject_id"),)

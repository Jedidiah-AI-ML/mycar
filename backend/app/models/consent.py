import uuid
from datetime import datetime

from sqlalchemy import ForeignKey, Index, String, func, text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.enums import ConsentPurpose, pg_enum


class Consent(Base):
    """One row per grant. Revoking sets `revoked_at`; rows are never deleted (audit trail)."""

    __tablename__ = "consents"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, server_default=func.gen_random_uuid())
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    purpose: Mapped[ConsentPurpose] = mapped_column(pg_enum(ConsentPurpose, "consent_purpose"))
    version: Mapped[str] = mapped_column(String(32))
    granted_at: Mapped[datetime] = mapped_column(server_default=func.now())
    revoked_at: Mapped[datetime | None]

    __table_args__ = (
        Index(
            "uq_consents_active_purpose",
            "user_id",
            "purpose",
            unique=True,
            postgresql_where=text("revoked_at IS NULL"),
        ),
    )

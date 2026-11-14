import uuid
from datetime import datetime

from sqlalchemy import Boolean, ForeignKey, Index, String, func, text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin


class User(TimestampMixin, Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, server_default=func.gen_random_uuid())
    email: Mapped[str] = mapped_column(String(320))
    password_hash: Mapped[str] = mapped_column(String(255))
    display_name: Mapped[str] = mapped_column(String(100))
    timezone: Mapped[str] = mapped_column(String(64), server_default="Africa/Lagos")
    is_admin: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    is_demo: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    email_verified_at: Mapped[datetime | None]
    deleted_at: Mapped[datetime | None]

    __table_args__ = (
        # Case-insensitive uniqueness: "A@x.com" and "a@x.com" are the same person.
        Index("uq_users_email_lower", func.lower(email), unique=True),
    )


class RefreshToken(Base):
    __tablename__ = "refresh_tokens"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, server_default=func.gen_random_uuid())
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)  # sha256 hex
    issued_at: Mapped[datetime] = mapped_column(server_default=func.now())
    expires_at: Mapped[datetime]
    revoked_at: Mapped[datetime | None]
    replaced_by: Mapped[uuid.UUID | None]
    user_agent: Mapped[str | None] = mapped_column(String(255))

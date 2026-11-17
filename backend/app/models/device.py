"""Devices, which car each is in, commands for them, and the batch idempotency record."""

import uuid
from datetime import date, datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    ForeignKey,
    Identity,
    Index,
    Integer,
    SmallInteger,
    String,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin
from app.models.enums import DeviceCommandStatus, DeviceCommandType, pg_enum

# Registered device adapters (PRD 7, ADR-002). A new adapter adds itself here (migration).
DEVICE_TYPES = ("freematics_one_plus",)


class Device(TimestampMixin, Base):
    __tablename__ = "devices"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)  # e.g. FM1-0001
    type: Mapped[str] = mapped_column(String(32))
    token_hash: Mapped[str] = mapped_column(String(64))  # sha256 hex of the device secret
    claim_code_hash: Mapped[str | None] = mapped_column(String(64))
    firmware_version: Mapped[str | None] = mapped_column(String(32))
    capabilities: Mapped[dict] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    sim_iccid: Mapped[str | None] = mapped_column(String(22))
    last_seen_at: Mapped[datetime | None]
    last_rssi: Mapped[int | None] = mapped_column(SmallInteger)
    bytes_month: Mapped[int] = mapped_column(BigInteger, server_default=text("0"))
    bytes_month_start: Mapped[date | None]
    disabled_at: Mapped[datetime | None]  # lost / retired: uploads are refused

    __table_args__ = (
        CheckConstraint("id ~ '^[A-Z0-9][A-Z0-9-]{2,31}$'", name="id_format"),
        CheckConstraint(
            "type IN (" + ", ".join(f"'{t}'" for t in DEVICE_TYPES) + ")", name="known_type"
        ),
        CheckConstraint("token_hash ~ '^[0-9a-f]{64}$'", name="token_hash_format"),
        Index("uq_devices_token_hash", "token_hash", unique=True),
    )


class DeviceAssignment(Base):
    """Which car a device is plugged into. History is kept; one active row each way."""

    __tablename__ = "device_assignments"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    device_id: Mapped[str] = mapped_column(ForeignKey("devices.id", ondelete="CASCADE"))
    vehicle_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("vehicles.id", ondelete="CASCADE"))
    assigned_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    assigned_at: Mapped[datetime] = mapped_column(server_default=func.now())
    unassigned_at: Mapped[datetime | None]

    __table_args__ = (
        CheckConstraint(
            "unassigned_at IS NULL OR unassigned_at >= assigned_at", name="unassigned_after"
        ),
        # PRD 7: one device per car, and a device is in one car at a time.
        Index(
            "uq_device_assignments_device_active",
            "device_id",
            unique=True,
            postgresql_where=text("unassigned_at IS NULL"),
        ),
        Index(
            "uq_device_assignments_vehicle_active",
            "vehicle_id",
            unique=True,
            postgresql_where=text("unassigned_at IS NULL"),
        ),
    )


class DeviceCommand(Base):
    """Server -> device commands, delivered inside batch acks (ADR-006)."""

    __tablename__ = "device_commands"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    device_id: Mapped[str] = mapped_column(ForeignKey("devices.id", ondelete="CASCADE"))
    type: Mapped[DeviceCommandType] = mapped_column(
        pg_enum(DeviceCommandType, "device_command_type")
    )
    payload: Mapped[dict] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    status: Mapped[DeviceCommandStatus] = mapped_column(
        pg_enum(DeviceCommandStatus, "device_command_status"),
        server_default=DeviceCommandStatus.queued.value,
    )
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    sent_at: Mapped[datetime | None]
    done_at: Mapped[datetime | None]
    result: Mapped[dict | None] = mapped_column(JSONB)

    __table_args__ = (
        # The ack path asks "anything outstanding for this device?" on every batch.
        Index(
            "ix_device_commands_outstanding",
            "device_id",
            postgresql_where=text("status IN ('queued', 'sent')"),
        ),
    )


class DeviceBatch(Base):
    """One row per accepted batch: **the idempotency record** (ADR-006).

    Inserted in the same transaction as the batch's readings. A retried batch hits this
    primary key and is acknowledged as a duplicate without storing anything.
    """

    __tablename__ = "device_batches"

    device_id: Mapped[str] = mapped_column(
        ForeignKey("devices.id", ondelete="CASCADE"), primary_key=True
    )
    boot: Mapped[int] = mapped_column(Integer, primary_key=True)
    trip: Mapped[int] = mapped_column(Integer, primary_key=True)
    seq: Mapped[int] = mapped_column(Integer, primary_key=True)
    received_at: Mapped[datetime] = mapped_column(server_default=func.now())
    clock_offset_ms: Mapped[int | None] = mapped_column(BigInteger)  # applied skew correction
    n_samples: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    bytes: Mapped[int] = mapped_column(Integer, server_default=text("0"))  # gzip body size
    trip_id: Mapped[uuid.UUID | None]

    __table_args__ = (
        CheckConstraint("boot >= 0 AND trip >= 0 AND seq >= 0", name="counters_non_negative"),
        Index("ix_device_batches_received_at", "received_at"),  # 90-day retention sweep
    )

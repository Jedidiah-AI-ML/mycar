"""Trips and what was measured on them.

`readings` and `positions` are the big tables. They are plain Core tables (no primary
key, no foreign keys, ADR-003): rows go in with COPY, are read by time range, and are
deleted in bulk. Whoever deletes a vehicle must delete its readings and positions too
(the account purge job does).
"""

import uuid
from datetime import datetime

from sqlalchemy import (
    REAL,
    BigInteger,
    CheckConstraint,
    Column,
    DateTime,
    Double,
    ForeignKey,
    Identity,
    Index,
    Integer,
    SmallInteger,
    String,
    Table,
    Uuid,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin
from app.models.enums import DtcStatus, TripStatus, pg_enum


class Trip(TimestampMixin, Base):
    __tablename__ = "trips"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, server_default=func.gen_random_uuid())
    vehicle_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("vehicles.id", ondelete="CASCADE"))
    device_id: Mapped[str | None] = mapped_column(ForeignKey("devices.id", ondelete="SET NULL"))
    device_boot: Mapped[int]
    device_trip: Mapped[int]
    started_at: Mapped[datetime | None]
    ended_at: Mapped[datetime | None]
    status: Mapped[TripStatus] = mapped_column(
        pg_enum(TripStatus, "trip_status"), server_default=TripStatus.open.value
    )
    n_samples: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    completeness: Mapped[float | None] = mapped_column(REAL)  # 0..1, from the gap list
    gaps: Mapped[list] = mapped_column(JSONB, server_default=text("'[]'::jsonb"))

    __table_args__ = (
        # A device's trip is identified by its own counters; batches find their trip here.
        Index("uq_trips_device_trip", "device_id", "device_boot", "device_trip", unique=True),
        Index("ix_trips_vehicle_started", "vehicle_id", "started_at"),
        CheckConstraint(
            "completeness IS NULL OR (completeness >= 0 AND completeness <= 1)",
            name="completeness_range",
        ),
        CheckConstraint(
            "ended_at IS NULL OR started_at IS NULL OR ended_at >= started_at", name="end_after"
        ),
    )


class Signal(Base):
    """The signal catalogue (backend/app/config/signals.yaml). Ids are stable forever."""

    __tablename__ = "signals"

    id: Mapped[int] = mapped_column(SmallInteger, primary_key=True, autoincrement=False)
    name: Mapped[str] = mapped_column(String(32), unique=True)
    unit: Mapped[str] = mapped_column(String(16))
    pid: Mapped[str | None] = mapped_column(String(4))
    grp: Mapped[str] = mapped_column(String(16))  # fast / slow / start / device / motion


readings = Table(
    "readings",
    Base.metadata,
    Column("vehicle_id", Uuid, nullable=False),
    Column("trip_id", Uuid, nullable=True),
    Column("ts", DateTime(timezone=True), nullable=False),
    Column("signal_id", SmallInteger, nullable=False),
    Column("value", REAL, nullable=False),  # 4 bytes: enough for OBD precision (Q8)
    # BRIN: a tiny index that works because rows arrive roughly in time order.
    Index("ix_readings_ts_brin", "ts", postgresql_using="brin"),
    Index("ix_readings_trip_signal_ts", "trip_id", "signal_id", "ts"),
)

positions = Table(
    "positions",
    Base.metadata,
    Column("vehicle_id", Uuid, nullable=False),
    Column("trip_id", Uuid, nullable=True),
    Column("ts", DateTime(timezone=True), nullable=False),
    Column("lat", Double, nullable=False),
    Column("lon", Double, nullable=False),
    Column("speed_kmh", REAL, nullable=True),
    Index("ix_positions_ts_brin", "ts", postgresql_using="brin"),
    Index("ix_positions_trip_ts", "trip_id", "ts"),
    # Location consent withdrawn -> delete every position of the owner's vehicles (PRD 10).
    Index("ix_positions_vehicle_id", "vehicle_id"),
)


class DtcEvent(Base):
    """A code the car reported, by status. Interpreted in Phase 4."""

    __tablename__ = "dtc_events"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    vehicle_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("vehicles.id", ondelete="CASCADE"))
    code: Mapped[str] = mapped_column(String(5))
    status: Mapped[DtcStatus] = mapped_column(pg_enum(DtcStatus, "dtc_status"))
    first_seen: Mapped[datetime]
    last_seen: Mapped[datetime]
    cleared_at: Mapped[datetime | None]  # no longer reported by the car
    trip_id: Mapped[uuid.UUID | None]  # where it was first seen
    freeze_frame: Mapped[dict | None] = mapped_column(JSONB)

    __table_args__ = (
        CheckConstraint("code ~ '^[PCBU][0-9A-F]{4}$'", name="code_format"),
        CheckConstraint("last_seen >= first_seen", name="seen_order"),
        Index(
            "uq_dtc_events_active",
            "vehicle_id",
            "code",
            "status",
            unique=True,
            postgresql_where=text("cleared_at IS NULL"),
        ),
    )


class Mode06Result(Base):
    """Raw on-board test results (ADR-006: scaled and scored on the server in Phase 5)."""

    __tablename__ = "mode06_results"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    vehicle_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("vehicles.id", ondelete="CASCADE"))
    trip_id: Mapped[uuid.UUID | None]
    ts: Mapped[datetime]
    mid: Mapped[int] = mapped_column(SmallInteger)
    tid: Mapped[int] = mapped_column(SmallInteger)
    uasid: Mapped[int] = mapped_column(SmallInteger)  # unit and scaling id
    value: Mapped[int] = mapped_column(Integer)
    min: Mapped[int] = mapped_column(Integer)
    max: Mapped[int] = mapped_column(Integer)

    __table_args__ = (
        CheckConstraint(
            "mid BETWEEN 0 AND 255 AND tid BETWEEN 0 AND 255 AND uasid BETWEEN 0 AND 255",
            name="byte_ids",
        ),
        Index("ix_mode06_results_vehicle_test_ts", "vehicle_id", "mid", "tid", "ts"),
    )

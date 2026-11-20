"""Layer 1 maintenance: service items and rules (synced from config), what was done
(service records), and the odometer history the distance estimate is anchored to."""

import uuid
from datetime import date, datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    ForeignKey,
    Identity,
    Index,
    Integer,
    String,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.vehicle import MAX_ODOMETER_KM

ODOMETER_SOURCES = ("install", "user", "service", "obd_a6")


class ServiceItem(Base):
    """The 10 service items. Source of truth: app/config/service_rules.yaml (synced)."""

    __tablename__ = "service_items"

    key: Mapped[str] = mapped_column(String(32), primary_key=True)  # e.g. engine_oil
    name: Mapped[str] = mapped_column(String(64))


class ServiceRule(Base):
    """Intervals per item, profile, variant and scope. Synced from config (replaced as a
    whole on each sync); the API resolves rules from config, this copy is for SQL."""

    __tablename__ = "service_rules"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    item_key: Mapped[str] = mapped_column(ForeignKey("service_items.key", ondelete="CASCADE"))
    profile: Mapped[str] = mapped_column(String(32))
    variant: Mapped[str | None] = mapped_column(String(32))
    make: Mapped[str | None] = mapped_column(String(50))
    model: Mapped[str | None] = mapped_column(String(50))
    engine: Mapped[str | None] = mapped_column(String(80))
    interval_km: Mapped[int | None] = mapped_column(Integer)
    interval_months: Mapped[int] = mapped_column(Integer)

    __table_args__ = (
        CheckConstraint("interval_km IS NULL OR interval_km > 0", name="km_positive"),
        CheckConstraint("interval_months > 0", name="months_positive"),
        # One rule per item + profile + variant + scope; NULLs compare equal here, so two
        # default rules for the same item can't coexist.
        UniqueConstraint(
            "item_key",
            "profile",
            "variant",
            "make",
            "model",
            "engine",
            name="uq_service_rules_scope",
            postgresql_nulls_not_distinct=True,
        ),  # fmt: skip
    )


class ServiceRecord(Base):
    """A service done on a car: entered by the owner/manager, or onboarding history
    (`before_install`). The newest record per item resets that item's countdown."""

    __tablename__ = "service_records"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    vehicle_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("vehicles.id", ondelete="CASCADE"))
    item_key: Mapped[str] = mapped_column(ForeignKey("service_items.key", ondelete="RESTRICT"))
    done_on: Mapped[date]
    odometer_km: Mapped[int | None] = mapped_column(Integer)
    before_install: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    notes: Mapped[str | None] = mapped_column(String(500))
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())

    __table_args__ = (
        # Not-in-the-future is checked by the API (a CHECK on CURRENT_DATE breaks restores).
        CheckConstraint("done_on >= DATE '1990-01-01'", name="done_on_plausible"),
        CheckConstraint(
            f"odometer_km IS NULL OR (odometer_km >= 0 AND odometer_km <= {MAX_ODOMETER_KM})",
            name="odometer_range",
        ),
        Index("ix_service_records_vehicle_item_done", "vehicle_id", "item_key", "done_on"),
    )


class OdometerReading(Base):
    """Every odometer value we were told or read. The newest trusted one is the anchor the
    estimate counts trip distances from; a big disagreement is flagged, not trusted."""

    __tablename__ = "odometer_readings"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    vehicle_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("vehicles.id", ondelete="CASCADE"))
    ts: Mapped[datetime] = mapped_column(server_default=func.now())
    km: Mapped[int] = mapped_column(Integer)
    source: Mapped[str] = mapped_column(String(16))
    estimate_km_at_entry: Mapped[int | None] = mapped_column(Integer)  # our estimate then
    mismatch: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )

    __table_args__ = (
        CheckConstraint(f"km >= 0 AND km <= {MAX_ODOMETER_KM}", name="km_range"),
        CheckConstraint(
            "source IN (" + ", ".join(f"'{s}'" for s in ODOMETER_SOURCES) + ")",
            name="source_known",
        ),
        Index("ix_odometer_readings_vehicle_ts", "vehicle_id", "ts"),
    )

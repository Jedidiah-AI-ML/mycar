import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import CheckConstraint, ForeignKey, Index, Numeric, SmallInteger, String, func, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin
from app.models.enums import VinSource, pg_enum

# SEED - validate (S5, S6, S7): see docs/plans/phase1-plan.md
MIN_MODEL_YEAR = 1996  # OBD-II mandatory in the US from MY1996
MAX_ODOMETER_KM = 1_500_000


class Vehicle(TimestampMixin, Base):
    __tablename__ = "vehicles"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, server_default=func.gen_random_uuid())
    vin: Mapped[str | None] = mapped_column(String(17))
    vin_source: Mapped[VinSource] = mapped_column(
        pg_enum(VinSource, "vin_source"), server_default=VinSource.manual.value
    )
    make: Mapped[str] = mapped_column(String(50))
    model: Mapped[str] = mapped_column(String(50))
    year: Mapped[int] = mapped_column(SmallInteger)
    engine: Mapped[str | None] = mapped_column(String(80))  # e.g. "2ZR-FE 1.8L I4"
    displacement_l: Mapped[Decimal | None] = mapped_column(Numeric(3, 1))
    cylinders: Mapped[int | None] = mapped_column(SmallInteger)
    fuel_type: Mapped[str] = mapped_column(String(16), server_default="petrol")
    protocol: Mapped[str | None] = mapped_column(String(32))  # filled by the device (Phase 2)
    supported_pids: Mapped[dict | None] = mapped_column(JSONB)  # filled by the device (Phase 2)
    nickname: Mapped[str | None] = mapped_column(String(50))
    odometer_at_install_km: Mapped[int | None]
    installed_at: Mapped[date | None]
    # Who added it (informational). Ownership lives in vehicle_access, not here.
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    deleted_at: Mapped[datetime | None]

    __table_args__ = (
        CheckConstraint("fuel_type = 'petrol'", name="petrol_only_v1"),
        CheckConstraint(f"year >= {MIN_MODEL_YEAR} AND year <= 2100", name="year_range"),
        CheckConstraint(
            f"odometer_at_install_km IS NULL OR "
            f"(odometer_at_install_km >= 0 AND odometer_at_install_km <= {MAX_ODOMETER_KM})",
            name="odometer_range",
        ),
        CheckConstraint(
            "cylinders IS NULL OR (cylinders BETWEEN 1 AND 16)", name="cylinders_range"
        ),
        CheckConstraint(
            "displacement_l IS NULL OR (displacement_l > 0 AND displacement_l < 10)",
            name="displacement_range",
        ),
        Index(
            "uq_vehicles_vin_active",
            "vin",
            unique=True,
            postgresql_where=text("deleted_at IS NULL AND vin IS NOT NULL"),
        ),
    )

import uuid
from datetime import date, datetime

from pydantic import BaseModel, ConfigDict, Field

from app.models.vehicle import MAX_ODOMETER_KM


class ServiceRecordIn(BaseModel):
    item_key: str = Field(max_length=32, examples=["engine_oil"])
    done_on: date
    odometer_km: int | None = Field(default=None, ge=0, le=MAX_ODOMETER_KM)
    before_install: bool = False
    notes: str | None = Field(default=None, max_length=500)


class ServiceRecordOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    item_key: str
    done_on: date
    odometer_km: int | None
    before_install: bool
    notes: str | None
    created_by: uuid.UUID | None
    created_at: datetime


class MismatchOut(BaseModel):
    entered_km: int
    estimate_km: int
    diff_km: int
    threshold_km: int
    flagged: bool


class ServiceRecordCreated(ServiceRecordOut):
    odometer_check: MismatchOut | None = None


class OdometerIn(BaseModel):
    km: int = Field(ge=0, le=MAX_ODOMETER_KM)


class OdometerOut(BaseModel):
    id: int
    km: int
    source: str
    ts: datetime
    estimate_km_at_entry: int | None
    mismatch: bool
    check: MismatchOut | None
    message: str


class ItemOut(BaseModel):
    item: str
    name: str
    status: str  # overdue / due / soon / ok / unknown
    due_date: date | None
    due_by: str | None  # km / time
    days_left: int | None
    remaining_km: int | None
    interval_km: int | None  # after any wear multiplier
    base_interval_km: int | None
    interval_months: int
    wear_factor: float
    rule_scope: str  # default, or e.g. "Toyota / Corolla"
    oil_variant: str | None = None
    oil_variant_assumed: bool = False
    last_service: date | None
    reasons: list[str]


class OdometerSummary(BaseModel):
    estimate_km: int | None
    anchor_source: str | None
    anchor_at: datetime | None
    km_since_anchor: float
    partial_trips: int
    last_entry_mismatch: bool  # the newest owner entry disagreed with the estimate


class UsageSummary(BaseModel):
    km_per_day: float
    method: str  # mean30 / blend / prior
    days_of_data: float


class WearSummary(BaseModel):
    window_days: int
    km_in_window: float
    harsh_brakes_per_100km: float | None
    harsh_accels_per_100km: float | None
    idle_share: float | None


class MaintenanceOut(BaseModel):
    vehicle_id: uuid.UUID
    today: date
    odometer: OdometerSummary
    usage: UsageSummary
    wear: WearSummary
    items: list[ItemOut]  # most urgent first


class TripOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    started_at: datetime | None
    ended_at: datetime | None
    status: str
    end_reason: str | None
    distance_km: float | None
    distance_source: str | None
    driving_s: int | None
    idle_s: int | None
    idle_share: float | None
    max_speed_kmh: float | None
    harsh_brake_n: int
    harsh_accel_n: int
    completeness: float | None

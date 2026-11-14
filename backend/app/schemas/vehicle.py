import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator

from app.models import VehicleRole, VinSource
from app.models.vehicle import MAX_ODOMETER_KM, MIN_MODEL_YEAR
from app.services.vin import normalise_vin


class _VehicleFields(BaseModel):
    @field_validator("vin", mode="before", check_fields=False)
    @classmethod
    def _vin(cls, v: object) -> object:
        if v is None or v == "":
            return None
        if not isinstance(v, str):
            raise ValueError("VIN must be a string")
        return normalise_vin(v)

    @field_validator("year", check_fields=False)
    @classmethod
    def _year(cls, v: int | None) -> int | None:
        if v is not None and v > date.today().year + 1:
            raise ValueError(f"year can't be later than {date.today().year + 1}")
        return v


class VehicleCreate(_VehicleFields):
    vin: str | None = None
    make: str = Field(min_length=1, max_length=50)
    model: str = Field(min_length=1, max_length=50)
    year: int = Field(ge=MIN_MODEL_YEAR)
    engine: str | None = Field(default=None, max_length=80)
    displacement_l: Decimal | None = Field(default=None, gt=0, lt=10, decimal_places=1)
    cylinders: int | None = Field(default=None, ge=1, le=16)
    fuel_type: Literal["petrol"] = "petrol"  # v1 is petrol only (PRD 1)
    nickname: str | None = Field(default=None, max_length=50)
    odometer_at_install_km: int | None = Field(default=None, ge=0, le=MAX_ODOMETER_KM)
    installed_at: date | None = None


class VehicleUpdate(_VehicleFields):
    """All optional; only the fields sent are changed."""

    vin: str | None = None
    make: str | None = Field(default=None, min_length=1, max_length=50)
    model: str | None = Field(default=None, min_length=1, max_length=50)
    year: int | None = Field(default=None, ge=MIN_MODEL_YEAR)
    engine: str | None = Field(default=None, max_length=80)
    displacement_l: Decimal | None = Field(default=None, gt=0, lt=10, decimal_places=1)
    cylinders: int | None = Field(default=None, ge=1, le=16)
    nickname: str | None = Field(default=None, max_length=50)
    odometer_at_install_km: int | None = Field(default=None, ge=0, le=MAX_ODOMETER_KM)
    installed_at: date | None = None


class VehicleOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    vin: str | None
    vin_source: VinSource
    make: str
    model: str
    year: int
    engine: str | None
    displacement_l: Decimal | None
    cylinders: int | None
    fuel_type: str
    nickname: str | None
    odometer_at_install_km: int | None
    installed_at: date | None
    created_at: datetime
    updated_at: datetime


class VehicleWithRole(VehicleOut):
    my_role: VehicleRole
    warnings: list[str] = []


class AccessGrant(BaseModel):
    email: EmailStr
    role: Literal["viewer", "manager"]  # ownership moves only by transfer (later phase)


class AccessUpdate(BaseModel):
    role: Literal["viewer", "manager"]


class AccessOut(BaseModel):
    user_id: uuid.UUID
    email: str
    display_name: str
    role: VehicleRole
    granted_at: datetime
    granted_by: uuid.UUID | None

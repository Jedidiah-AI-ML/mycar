from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, BeforeValidator, Field, StringConstraints

from app.models.device import DEVICE_TYPES

# Normalise first (strip, upper-case), then check: "fm1-0100 " -> "FM1-0100".
DeviceIdIn = Annotated[
    str,
    BeforeValidator(lambda v: v.strip().upper() if isinstance(v, str) else v),
    StringConstraints(pattern=r"^[A-Z0-9][A-Z0-9-]{2,31}$"),
]


class DeviceProvision(BaseModel):
    id: DeviceIdIn | None = Field(default=None, description="Omit to get the next FM1-NNNN id")
    type: Literal[DEVICE_TYPES] = "freematics_one_plus"  # type: ignore[valid-type]
    sim_iccid: str | None = Field(default=None, pattern=r"^[0-9]{18,22}F?$")


class DeviceProvisioned(BaseModel):
    """Returned once. Neither the token nor the claim code can be read back later."""

    id: str
    type: str
    token: str = Field(description="Flash into the firmware. Shown only now.")
    claim_code: str = Field(description="Print on the label for the owner. Shown only now.")
    label_qr: str = Field(description="Text for the label's QR code")


class DeviceClaim(BaseModel):
    device_id: DeviceIdIn
    claim_code: str = Field(min_length=8, max_length=16)


class DeviceStatus(BaseModel):
    device_id: str
    type: str
    assigned_at: datetime
    online: bool
    last_seen_at: datetime | None
    last_rssi: int | None
    signal: Literal["excellent", "good", "fair", "poor", "unknown"]
    firmware_version: str | None
    data_used_this_month_bytes: int
    capabilities: dict[str, bool]

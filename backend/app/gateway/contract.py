"""Device payload contract v1 (ADR-006).

This module is the single source of truth for what a device sends and receives. The
simulator, and later the AutoCare firmware, build against the JSON Schema exported from
these models (`contracts/freematics_one_plus.v1.schema.json`):

    uv run python -m app.gateway.contract        # rewrites contracts/

Shape of a batch, in short:
- Columnar samples: each signal name appears once, followed by `[dt_ms, value]` pairs,
  where `dt_ms` is milliseconds after `t0` (the batch's base time on the device clock).
  That's far smaller than repeating the timestamp and name for every reading.
- The idempotency key is `(device_id, boot, trip, seq)`. A retried batch is recognised
  by it and never stored twice.
- Location goes in `g`, apart from engine signals, so it can be dropped at the door when
  the owner hasn't consented to location (PRD 10).
"""

import json
import math
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# --- limits (SEED - validate P10) -------------------------------------------------
MAX_SAMPLES_PER_SIGNAL = 3_600  # one hour at 1 Hz in one batch, far above normal
MAX_SIGNALS_PER_BATCH = 64
MAX_EVENTS_PER_BATCH = 200
MAX_BATCH_SPAN_MS = 6 * 3600 * 1000  # samples within 6 h of t0

DeviceId = Annotated[str, Field(pattern=r"^[A-Z0-9][A-Z0-9-]{2,31}$", examples=["FM1-0001"])]
SignalName = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]{0,31}$")]
DtcCode = Annotated[str, Field(pattern=r"^[PCBU][0-9A-F]{4}$", examples=["P0128"])]
EpochMs = Annotated[int, Field(ge=0, description="Unix time in milliseconds")]
DtMs = Annotated[int, Field(ge=0, le=MAX_BATCH_SPAN_MS)]
Sample = tuple[DtMs, float]


class _Strict(BaseModel):
    # Unknown *fields* are rejected so typos surface in testing. Unknown *signal names*
    # and *event types* are accepted elsewhere for forward compatibility.
    model_config = ConfigDict(extra="forbid")


# ---------------------------------------------------------------- events


class _Event(_Strict):
    t: DtMs = Field(description="ms after the batch's t0")


class TripStart(_Event):
    type: Literal["trip_start"]


class TripEnd(_Event):
    type: Literal["trip_end"]
    reason: Literal["ignition_off", "idle_timeout", "power_loss", "other"] = "ignition_off"


class VinEvent(_Event):
    type: Literal["vin"]
    vin: str = Field(min_length=11, max_length=17)


class PidsSupported(_Event):
    type: Literal["pids_supported"]
    protocol: str = Field(max_length=32, examples=["ISO 15765-4 CAN 11/500"])
    mode01: str = Field(
        pattern=r"^[0-9A-F]{8,64}$",
        description="Concatenated Mode 01 support bitmaps (PIDs 00, 20, 40, ...) as hex",
    )


class Readiness(_Event):
    type: Literal["readiness"]
    mil: bool
    dtc_count: int = Field(ge=0, le=127)
    monitors: dict[str, Literal["complete", "incomplete", "na"]] = Field(
        description="Monitor name -> state, from Mode 01 PID 01"
    )


class DtcEvent(_Event):
    type: Literal["dtc"]
    mode: Literal["03", "07", "0A"] = Field(description="03 stored, 07 pending, 0A permanent")
    codes: list[DtcCode] = Field(max_length=64)


class FreezeFrame(_Event):
    type: Literal["freeze_frame"]
    code: DtcCode
    values: dict[SignalName, float]


class Mode06Result(_Strict):
    mid: int = Field(ge=0, le=255)
    tid: int = Field(ge=0, le=255)
    uasid: int = Field(ge=0, le=255, description="Unit and scaling id (raw values below)")
    value: int
    min: int
    max: int


class Mode06Event(_Event):
    type: Literal["mode06"]
    results: list[Mode06Result] = Field(max_length=512)


class CrankEvent(_Event):
    type: Literal["crank"]
    rate_hz: float = Field(gt=0, le=10_000)
    volts: list[float] = Field(max_length=4096, description="Battery volts at rate_hz")


class HarshEvent(_Event):
    type: Literal["harsh"]
    kind: Literal["brake", "accel", "corner"]
    peak_g: float = Field(ge=0, le=16)


class CommandResult(_Event):
    type: Literal["cmd_result"]
    command_id: int
    status: Literal["done", "failed", "unsupported"]
    detail: str | None = Field(default=None, max_length=500)


class UnknownEvent(BaseModel):
    """Event types newer than this server: kept out of the way, never a rejection."""

    model_config = ConfigDict(extra="allow")
    t: DtMs
    type: Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]{0,31}$")]

    @field_validator("type")
    @classmethod
    def _not_a_known_type(cls, v: str) -> str:
        # A *known* event with a bad field must fail, not hide here as "unknown".
        if v in KNOWN_EVENT_TYPES:
            raise ValueError(f"invalid {v!r} event")
        return v


KNOWN_EVENTS = (
    TripStart, TripEnd, VinEvent, PidsSupported, Readiness, DtcEvent, FreezeFrame,
    Mode06Event, CrankEvent, HarshEvent, CommandResult,
)  # fmt: skip
KNOWN_EVENT_TYPES = {m.model_fields["type"].annotation.__args__[0] for m in KNOWN_EVENTS}
KnownEvent = Annotated[
    TripStart | TripEnd | VinEvent | PidsSupported | Readiness | DtcEvent | FreezeFrame
    | Mode06Event | CrankEvent | HarshEvent | CommandResult,
    Field(discriminator="type"),
]  # fmt: skip


# ---------------------------------------------------------------- batch


class Network(_Strict):
    rssi: int | None = Field(default=None, ge=-150, le=0, description="dBm")
    bytes_up: int | None = Field(default=None, ge=0, description="Bytes uploaded since boot")
    iccid: str | None = Field(default=None, pattern=r"^[0-9]{18,22}F?$")


class BatchV1(_Strict):
    v: Literal[1]
    device_id: DeviceId
    fw: str = Field(max_length=32)
    boot: int = Field(ge=0, description="Boot counter, persisted on the device")
    trip: int = Field(ge=0, description="Trip counter, persisted on the device")
    seq: int = Field(ge=0, description="Batch number within the trip, from 0")
    final: bool = Field(default=False, description="Last batch of the trip")
    t0: EpochMs = Field(description="Base time of this batch, device clock")
    sent_at: EpochMs = Field(description="Device clock when this upload was attempted")
    gnss_time: EpochMs | None = Field(
        default=None, description="GNSS time at sent_at, if the device has a fix"
    )
    s: dict[SignalName, list[Sample]] = Field(default_factory=dict)
    g: list[tuple[DtMs, float, float, float]] = Field(
        default_factory=list, description="[dt_ms, lat, lon, speed_kmh] fixes"
    )
    ev: list[KnownEvent | UnknownEvent] = Field(default_factory=list)
    net: Network = Field(default_factory=Network)

    @field_validator("s")
    @classmethod
    def _check_samples(cls, s: dict[str, list[Sample]]) -> dict[str, list[Sample]]:
        if len(s) > MAX_SIGNALS_PER_BATCH:
            raise ValueError(f"at most {MAX_SIGNALS_PER_BATCH} signals per batch")
        for name, rows in s.items():
            if len(rows) > MAX_SAMPLES_PER_SIGNAL:
                raise ValueError(f"{name}: at most {MAX_SAMPLES_PER_SIGNAL} samples")
            last = -1
            for dt, value in rows:
                if dt < last:
                    raise ValueError(f"{name}: dt_ms must be non-decreasing")
                if not math.isfinite(value):
                    raise ValueError(f"{name}: values must be finite")
                last = dt
        return s

    @field_validator("g")
    @classmethod
    def _check_fixes(cls, g: list[tuple[int, float, float, float]]) -> list:
        for _, lat, lon, spd in g:
            if not (-90 <= lat <= 90 and -180 <= lon <= 180 and 0 <= spd <= 400):
                raise ValueError("GNSS fix out of range")
        return g

    @field_validator("ev")
    @classmethod
    def _check_events(cls, ev: list) -> list:
        if len(ev) > MAX_EVENTS_PER_BATCH:
            raise ValueError(f"at most {MAX_EVENTS_PER_BATCH} events per batch")
        return ev

    @model_validator(mode="after")
    def _plausible_clock(self) -> "BatchV1":
        if self.sent_at + 1000 < self.t0:
            raise ValueError("sent_at is before t0")
        return self

    @property
    def key(self) -> tuple[str, int, int, int]:
        """Idempotency key: a retried batch has the same key."""
        return (self.device_id, self.boot, self.trip, self.seq)


# ---------------------------------------------------------------- critical packet


class CriticalV1(_Strict):
    """Small, sent immediately (not batched) when the device sees a Stop-safely condition."""

    v: Literal[1]
    device_id: DeviceId
    boot: int = Field(ge=0)
    trip: int = Field(ge=0)
    t: EpochMs = Field(description="Device clock when the condition was detected")
    sent_at: EpochMs
    kind: Literal["coolant_high", "voltage_collapse", "misfire_severe", "other"]
    values: dict[SignalName, float] = Field(max_length=16)
    pos: tuple[float, float] | None = Field(default=None, description="[lat, lon] if consented")


# ---------------------------------------------------------------- server -> device


class SetPollRates(_Strict):
    fast_hz: float = Field(gt=0, le=10)
    slow_s: float = Field(ge=1, le=60)


class Command(_Strict):
    id: int
    type: Literal["health_scan", "set_poll_rates"]
    payload: dict = Field(default_factory=dict)


class AckV1(_Strict):
    ok: bool = True
    dup: bool = Field(default=False, description="This batch key was already stored")
    server_time: EpochMs = Field(description="Lets the device correct its clock")
    cmds: list[Command] = Field(default_factory=list)


# ---------------------------------------------------------------- export

REPO_ROOT = Path(__file__).resolve().parents[3]
CONTRACTS_DIR = REPO_ROOT / "contracts"
SCHEMA_FILE = CONTRACTS_DIR / "freematics_one_plus.v1.schema.json"
SIGNALS_EXPORT = CONTRACTS_DIR / "signals.v1.json"


def schema_document() -> dict:
    defs: dict = {}
    roots = {}
    for name, model in (("batch", BatchV1), ("critical", CriticalV1), ("ack", AckV1)):
        sch = model.model_json_schema(ref_template="#/$defs/{model}")
        defs.update(sch.pop("$defs", {}))
        defs[model.__name__] = sch
        roots[name] = {"$ref": f"#/$defs/{model.__name__}"}
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": "https://autocare.local/contracts/freematics_one_plus.v1.schema.json",
        "title": "AutoCare device payload contract v1 (ADR-006)",
        "description": "Generated from backend/app/gateway/contract.py. Do not edit by hand.",
        "oneOf": list(roots.values()),
        "$defs": dict(sorted(defs.items())),
    }


def signals_document() -> dict:
    from app.gateway.signals import catalogue

    cat = catalogue()
    return {
        "version": cat.version,
        "description": "Generated from backend/app/config/signals.yaml. Do not edit by hand.",
        "signals": [s.__dict__ for s in cat.signals],
    }


def render(doc: dict) -> str:
    return json.dumps(doc, indent=2, ensure_ascii=False, sort_keys=False) + "\n"


def export() -> None:
    CONTRACTS_DIR.mkdir(exist_ok=True)
    # newline="\n": identical bytes on Windows and Linux (no CRLF in contracts/).
    SCHEMA_FILE.write_text(render(schema_document()), encoding="utf-8", newline="\n")
    SIGNALS_EXPORT.write_text(render(signals_document()), encoding="utf-8", newline="\n")
    print(f"wrote {SCHEMA_FILE.relative_to(REPO_ROOT)} and {SIGNALS_EXPORT.relative_to(REPO_ROOT)}")


if __name__ == "__main__":
    export()

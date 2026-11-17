"""Enums stored as native Postgres enum types."""

import enum

from sqlalchemy import Enum


class ConsentPurpose(enum.StrEnum):
    service_processing = "service_processing"  # required to use AutoCare at all
    location = "location"  # GNSS on the device; optional, features degrade
    model_improvement = "model_improvement"  # pseudonymised data for model training


class VehicleRole(enum.StrEnum):
    viewer = "viewer"
    manager = "manager"
    owner = "owner"

    @property
    def rank(self) -> int:
        return {"viewer": 1, "manager": 2, "owner": 3}[self.value]

    def at_least(self, other: "VehicleRole") -> bool:
        return self.rank >= other.rank


class VinSource(enum.StrEnum):
    manual = "manual"
    device = "device"


class JobStatus(enum.StrEnum):
    queued = "queued"
    running = "running"
    succeeded = "succeeded"
    failed = "failed"  # will retry
    dead = "dead"  # out of attempts


class DeviceCommandType(enum.StrEnum):
    health_scan = "health_scan"
    set_poll_rates = "set_poll_rates"


class DeviceCommandStatus(enum.StrEnum):
    queued = "queued"  # waiting for the device's next upload
    sent = "sent"  # handed out in an ack
    done = "done"
    failed = "failed"  # device answered failed / unsupported


class TripStatus(enum.StrEnum):
    open = "open"  # data still arriving
    complete = "complete"
    late_data = "late_data"  # a batch arrived after it was complete; re-assembled


class DtcStatus(enum.StrEnum):
    stored = "stored"  # Mode 03 (confirmed, MIL)
    pending = "pending"  # Mode 07
    permanent = "permanent"  # Mode 0A


def pg_enum(cls: type[enum.Enum], name: str) -> Enum:
    return Enum(cls, name=name, values_callable=lambda e: [m.value for m in e])

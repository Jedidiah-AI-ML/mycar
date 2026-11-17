"""Importing this package registers every model on `Base.metadata` (Alembic relies on it)."""

from app.models.access import AuditEvent, VehicleAccess
from app.models.consent import Consent
from app.models.device import Device, DeviceAssignment, DeviceBatch, DeviceCommand
from app.models.enums import (
    ConsentPurpose,
    DeviceCommandStatus,
    DeviceCommandType,
    DtcStatus,
    JobStatus,
    TripStatus,
    VehicleRole,
    VinSource,
)
from app.models.job import Job
from app.models.telemetry import DtcEvent, Mode06Result, Signal, Trip, positions, readings
from app.models.user import RefreshToken, User
from app.models.vehicle import Vehicle

__all__ = [
    "AuditEvent",
    "Consent",
    "ConsentPurpose",
    "Device",
    "DeviceAssignment",
    "DeviceBatch",
    "DeviceCommand",
    "DeviceCommandStatus",
    "DeviceCommandType",
    "DtcEvent",
    "DtcStatus",
    "Job",
    "JobStatus",
    "Mode06Result",
    "RefreshToken",
    "Signal",
    "Trip",
    "TripStatus",
    "User",
    "Vehicle",
    "VehicleAccess",
    "VehicleRole",
    "VinSource",
    "positions",
    "readings",
]

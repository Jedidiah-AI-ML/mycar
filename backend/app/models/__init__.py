"""Importing this package registers every model on `Base.metadata` (Alembic relies on it)."""

from app.models.access import AuditEvent, VehicleAccess
from app.models.consent import Consent
from app.models.device import Device, DeviceAssignment, DeviceBatch, DeviceCommand
from app.models.dtc import DtcDefinition, Insight, LlmCall
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
from app.models.maintenance import OdometerReading, ServiceItem, ServiceRecord, ServiceRule
from app.models.telemetry import (
    DtcEvent,
    Mode06Result,
    Signal,
    Trip,
    positions,
    reading_minutes,
    readings,
)
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
    "DtcDefinition",
    "DtcEvent",
    "DtcStatus",
    "Insight",
    "Job",
    "JobStatus",
    "LlmCall",
    "Mode06Result",
    "OdometerReading",
    "RefreshToken",
    "ServiceItem",
    "ServiceRecord",
    "ServiceRule",
    "Signal",
    "Trip",
    "TripStatus",
    "User",
    "Vehicle",
    "VehicleAccess",
    "VehicleRole",
    "VinSource",
    "positions",
    "reading_minutes",
    "readings",
]

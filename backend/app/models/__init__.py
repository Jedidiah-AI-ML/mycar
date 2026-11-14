"""Importing this package registers every model on `Base.metadata` (Alembic relies on it)."""

from app.models.access import AuditEvent, VehicleAccess
from app.models.consent import Consent
from app.models.enums import ConsentPurpose, JobStatus, VehicleRole, VinSource
from app.models.job import Job
from app.models.user import RefreshToken, User
from app.models.vehicle import Vehicle

__all__ = [
    "AuditEvent",
    "Consent",
    "ConsentPurpose",
    "Job",
    "JobStatus",
    "RefreshToken",
    "User",
    "Vehicle",
    "VehicleAccess",
    "VehicleRole",
    "VinSource",
]

from datetime import datetime

from pydantic import BaseModel, ConfigDict

from app.models import ConsentPurpose


class ConsentRecord(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    purpose: ConsentPurpose
    version: str
    granted_at: datetime
    revoked_at: datetime | None


class ConsentStatus(BaseModel):
    purpose: ConsentPurpose
    title: str
    text: str
    required: bool
    active: bool
    granted_version: str | None
    needs_reconsent: bool  # granted, but to an older text version


class ConsentsOut(BaseModel):
    current_version: str
    purposes: list[ConsentStatus]
    history: list[ConsentRecord]

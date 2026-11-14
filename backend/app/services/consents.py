"""Consent texts (versioned YAML) and grant/revoke logic."""

from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path
from uuid import UUID

import yaml
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Consent, ConsentPurpose
from app.services import audit

CONSENTS_FILE = Path(__file__).resolve().parents[1] / "config" / "consents.yaml"


@lru_cache
def consent_config() -> dict:
    data = yaml.safe_load(CONSENTS_FILE.read_text(encoding="utf-8"))
    missing = {p.value for p in ConsentPurpose} - set(data["purposes"])
    if missing:
        raise RuntimeError(f"consents.yaml is missing purposes: {sorted(missing)}")
    return data


def current_version() -> str:
    return consent_config()["version"]


def required_purposes() -> set[ConsentPurpose]:
    return {ConsentPurpose(k) for k, v in consent_config()["purposes"].items() if v["required"]}


async def active_consent(
    session: AsyncSession, user_id: UUID, purpose: ConsentPurpose
) -> Consent | None:
    return await session.scalar(
        select(Consent).where(
            Consent.user_id == user_id, Consent.purpose == purpose, Consent.revoked_at.is_(None)
        )
    )


async def grant(session: AsyncSession, user_id: UUID, purpose: ConsentPurpose) -> Consent:
    """Idempotent: granting an already-active purpose at the current version is a no-op."""
    existing = await active_consent(session, user_id, purpose)
    if existing and existing.version == current_version():
        return existing
    if existing:  # re-consent to a newer text: close the old grant first
        existing.revoked_at = datetime.now(UTC)
        await session.flush()
    c = Consent(user_id=user_id, purpose=purpose, version=current_version())
    session.add(c)
    await session.flush()
    await audit.record(
        session,
        user_id,
        "consent.granted",
        "user",
        user_id,
        purpose=purpose.value,
        version=c.version,
    )
    return c


async def revoke(session: AsyncSession, user_id: UUID, purpose: ConsentPurpose) -> bool:
    existing = await active_consent(session, user_id, purpose)
    if not existing:
        return False
    existing.revoked_at = datetime.now(UTC)
    await audit.record(session, user_id, "consent.revoked", "user", user_id, purpose=purpose.value)
    return True

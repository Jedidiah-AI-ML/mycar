"""Append-only audit trail for privacy-relevant actions (sharing, consents, deletion)."""

from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AuditEvent


async def record(
    session: AsyncSession,
    actor_user_id: UUID | None,
    action: str,
    subject_type: str,
    subject_id: UUID | str,
    **details: object,
) -> None:
    session.add(
        AuditEvent(
            actor_user_id=actor_user_id,
            action=action,
            subject_type=subject_type,
            subject_id=str(subject_id),
            details={k: str(v) if isinstance(v, UUID) else v for k, v in details.items()},
        )
    )

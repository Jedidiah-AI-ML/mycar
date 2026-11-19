"""Critical packets from the device (on-device "Stop safely" conditions, PRD 5).

Phase 2 records each one in the audit trail and the log, at priority 0. The real alert
rules, severity tiers and web push arrive in Phase 6 and replace the body of this job.
"""

import logging

from sqlalchemy.ext.asyncio import AsyncSession

from app.jobs.registry import job
from app.services import audit

log = logging.getLogger("app.jobs.alerts")


@job("alert.critical", max_attempts=10)
async def critical(payload: dict, session: AsyncSession) -> None:
    log.warning("critical condition reported by device", extra=payload)
    await audit.record(
        session, None, "device.critical", "vehicle", payload["vehicle_id"],
        kind=payload["kind"], device_id=payload["device_id"], ts_ms=payload["ts_ms"],
    )  # fmt: skip

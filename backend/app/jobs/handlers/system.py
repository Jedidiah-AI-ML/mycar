import logging

from sqlalchemy.ext.asyncio import AsyncSession

from app.jobs.registry import job

log = logging.getLogger("app.jobs.system")


@job("system.ping", max_attempts=1)
async def ping(payload: dict, session: AsyncSession) -> None:
    """Does nothing but log. Used to prove a deployed worker is picking up jobs."""
    log.info("pong", extra={"payload": payload})

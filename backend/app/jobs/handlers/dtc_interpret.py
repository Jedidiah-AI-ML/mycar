"""`dtc.interpret`: explain the car's active fault codes (Phase 4, ADR-013).

Runs after `trip.summarise` for every finished trip. Cheap when nothing changed: each
code's context is hashed, and a stored explanation for the same hash means no LLM call
(see app/services/dtc/interpret.py). Commits after each code, so LLM spend is never lost
to a rollback.
"""

import logging
import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.jobs.pipeline import next_stage
from app.jobs.queue import enqueue
from app.jobs.registry import job
from app.models import Trip
from app.services.dtc.interpret import interpret_vehicle

log = logging.getLogger("app.jobs.dtc_interpret")
NAME = "dtc.interpret"


@job(NAME, max_attempts=5)
async def interpret_trip_codes(payload: dict, session: AsyncSession) -> None:
    trip_id = uuid.UUID(payload["trip_id"])
    trip = await session.get(Trip, trip_id)
    if trip is None:
        return
    out = await interpret_vehicle(session, trip.vehicle_id, commit_each=True)
    if (nxt := next_stage(NAME)) is not None:
        await enqueue(session, nxt, {"trip_id": str(trip_id)}, dedupe_key=f"{nxt}:{trip_id}")
    log.info(
        "codes interpreted",
        extra={"trip_id": str(trip_id), "cached": len(out.cached),
               "personalised": len(out.personalised), "generic": len(out.generic),
               "template": len(out.template), "kept": len(out.kept)},
    )  # fmt: skip

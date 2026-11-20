"""`trip.assemble`: turn a trip's stored batches into one summary row.

Computed from what is in the database, never from arrival order, so the result is the
same however late or shuffled the batches came:
- start = earliest of the trip_start event and the first reading
- end   = the trip_end event (or the last reading, if later); if the device never sent
          trip_end (lost power), the trip closes after `trip_close_after_s` with no new
          batch (SEED P11, 15 min) and ends at its last reading
- samples per signal, total samples
- gaps  = holes longer than `trip_gap_s` in the 1 Hz RPM stream (coverage holes, lost
          batches), and completeness = 1 - gap time / trip time
- status: open -> complete; if a batch arrives after `complete` (the trip now has more
          batches than when it was assembled), this job re-runs and the status becomes
          `late_data` (everything else is simply recomputed)

Concurrency: the job locks the trip row first. A batch for this trip being stored at
the same moment waits for that lock, then queues a fresh assembly once this one has
committed, so no batch is ever left out.
"""

import logging
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.gateway.signals import catalogue
from app.jobs.pipeline import next_stage
from app.jobs.queue import enqueue
from app.jobs.registry import job
from app.models import DeviceBatch, Trip, TripStatus, readings

log = logging.getLogger("app.jobs.trips")
NAME = "trip.assemble"


def _ms(ts: datetime) -> int:
    return int(ts.timestamp() * 1000)


def find_gaps(ts_ms: list[int], gap_s: float) -> list[list[int]]:
    """[[from_ms, to_ms], ...] where consecutive samples are more than gap_s apart."""
    limit = gap_s * 1000
    return [[a, b] for a, b in zip(ts_ms, ts_ms[1:], strict=False) if b - a > limit]


@job(NAME, max_attempts=8)
async def assemble_trip(payload: dict, session: AsyncSession) -> None:
    s = get_settings()
    trip_id = uuid.UUID(payload["trip_id"])
    trip = await session.scalar(select(Trip).where(Trip.id == trip_id).with_for_update())
    if trip is None:
        return  # vehicle deleted since

    n_batches, last_rx = (
        await session.execute(
            select(func.count(), func.max(DeviceBatch.received_at)).where(
                DeviceBatch.trip_id == trip_id
            )
        )
    ).one()
    counts = dict(
        (
            await session.execute(
                select(readings.c.signal_id, func.count())
                .where(readings.c.trip_id == trip_id)
                .group_by(readings.c.signal_id)
            )
        ).all()
    )
    first, last = (
        await session.execute(
            select(func.min(readings.c.ts), func.max(readings.c.ts)).where(
                readings.c.trip_id == trip_id
            )
        )
    ).one()

    # Coverage holes, measured on the 1 Hz RPM stream (the densest signal otherwise).
    by_name = catalogue().by_name
    ref = by_name["rpm"].id if by_name["rpm"].id in counts else None
    if ref is None and counts:
        ref = max(counts, key=counts.get)
    ref_ts = []
    if ref is not None:
        ref_ts = [
            _ms(t)
            for t in (
                await session.scalars(
                    select(readings.c.ts)
                    .where(readings.c.trip_id == trip_id, readings.c.signal_id == ref)
                    .order_by(readings.c.ts)
                )
            ).all()
        ]
    gaps = find_gaps(ref_ts, s.trip_gap_s)

    # Start and end.
    candidates = [t for t in (trip.started_at, first) if t is not None]
    started = min(candidates) if candidates else None
    ends = [e for e in trip.events if e.get("type") == "trip_end"]
    ended, end_reason = None, None
    if ends:
        last_end = max(ends, key=lambda e: e["ts"])
        ended = datetime.fromtimestamp(last_end["ts"] / 1000, UTC)
        ended = max(ended, last) if last is not None else ended
        end_reason = last_end.get("reason", "ignition_off")
    elif last_rx is not None and datetime.now(UTC) - last_rx >= timedelta(
        seconds=s.trip_close_after_s
    ):
        ended, end_reason = last, "timeout"  # device went quiet: lost power, or out of range

    span_end = ended or last
    completeness = None
    if started is not None and span_end is not None and span_end > started:
        hole_ms = sum(b - a for a, b in gaps)
        duration_ms = _ms(span_end) - _ms(started)
        completeness = max(0.0, min(1.0, 1 - hole_ms / duration_ms))

    was, was_batches = trip.status, trip.n_batches
    if ended is None:
        status = TripStatus.open
    elif was == TripStatus.late_data or (was == TripStatus.complete and n_batches > trip.n_batches):
        status = TripStatus.late_data
    else:
        status = TripStatus.complete

    names = catalogue().by_id
    trip.started_at, trip.ended_at, trip.end_reason = started, ended, end_reason
    trip.n_samples, trip.n_batches = sum(counts.values()), n_batches
    trip.signal_counts = {names[k].name if k in names else str(k): v for k, v in counts.items()}
    trip.gaps, trip.completeness, trip.status = gaps, completeness, status
    trip.assembled_at, trip.assembled_through = datetime.now(UTC), last_rx

    if status == TripStatus.open and last_rx is not None:
        # Come back when the trip would time out, in case trip_end never arrives. The key
        # includes the time, so this job can schedule its own successor.
        due = last_rx + timedelta(seconds=s.trip_close_after_s + 1)
        await enqueue(
            session, NAME, {"trip_id": str(trip_id)}, run_at=due,
            dedupe_key=f"trip.close:{trip_id}:{int(due.timestamp())}",
        )  # fmt: skip
    elif (status != was or n_batches != was_batches) and (nxt := next_stage(NAME)) is not None:
        # Newly closed, or closed and re-assembled with more data (a second late batch
        # leaves the status at late_data, but the next stages must still re-run).
        await enqueue(session, nxt, {"trip_id": str(trip_id)}, dedupe_key=f"{nxt}:{trip_id}")

    log.info(
        "trip assembled",
        extra={"trip_id": str(trip_id), "status": status.value, "samples": trip.n_samples,
               "gaps": len(gaps), "completeness": completeness},
    )  # fmt: skip

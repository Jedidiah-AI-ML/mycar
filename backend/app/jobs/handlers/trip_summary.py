"""`trip.summarise`: distance, driving/idle time, harsh driving, per-minute summaries,
and retention. Runs after `trip.assemble` for a closed trip (and again when late data
re-assembles it). Every result is recomputed from the stored rows, so re-running is
harmless.

- **Distance:** `app.services.distance` (notebook 02's gap policy), with the GPS track
  only if the owner consented (positions exist only then).
- **Idle vs driving:** time between consecutive speed samples, split at 2 km/h.
- **Harsh events:** the device's accelerometer events, plus braking/acceleration seen in
  the 1 Hz speed curve; events of one kind within 3 s of each other count once.
- **Per-minute summaries:** n / mean / min / max per signal and minute (upserted).
- **Retention (S9):** raw readings and positions more than 180 days older than *this
  car's* newest trip are deleted. Per-minute summaries stay. Measured from the car's own
  data, not today's date, so a car that sits parked for months loses nothing.
"""

import logging
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.gateway.signals import catalogue
from app.jobs.pipeline import next_stage
from app.jobs.queue import enqueue
from app.jobs.registry import job
from app.models import Trip, TripStatus, positions, readings
from app.services.distance import Fix, settings, trip_distance

log = logging.getLogger("app.jobs.trip_summary")
NAME = "trip.summarise"

_MINUTES = text(
    """
    INSERT INTO reading_minutes (vehicle_id, trip_id, signal_id, minute, n, mean, min, max)
    SELECT vehicle_id, trip_id, signal_id, date_trunc('minute', ts), count(*), avg(value),
           min(value), max(value)
    FROM readings WHERE trip_id = :trip_id
    GROUP BY vehicle_id, trip_id, signal_id, date_trunc('minute', ts)
    ON CONFLICT (trip_id, signal_id, minute) DO UPDATE
    SET n = EXCLUDED.n, mean = EXCLUDED.mean, min = EXCLUDED.min, max = EXCLUDED.max
    """
)


def harsh_counts(t_s: list[float], v_kmh: list[float], device_events: list[dict], cfg: dict):
    """(brakes, accels): device events + speed-curve events, merged within merge_within_s."""
    h = cfg["harsh"]
    found: dict[str, list[float]] = {"brake": [], "accel": []}
    for e in device_events:
        if e.get("type") == "harsh" and e.get("kind") in found:
            found[e["kind"]].append(e["ts"] / 1000)
    for i in range(1, len(t_s)):
        dt = t_s[i] - t_s[i - 1]
        if not 0 < dt <= 2:  # a rate over a hole means nothing
            continue
        rate = (v_kmh[i] - v_kmh[i - 1]) / dt
        if rate <= -h["brake_kmh_per_s"]:
            found["brake"].append(t_s[i])
        elif rate >= h["accel_kmh_per_s"]:
            found["accel"].append(t_s[i])

    def merged(times: list[float]) -> int:
        n, last = 0, None
        for t in sorted(times):
            if last is None or t - last > h["merge_within_s"]:
                n += 1
            last = t
        return n

    return merged(found["brake"]), merged(found["accel"])


def idle_split(t_s: list[float], v_kmh: list[float], cfg: dict) -> tuple[float, float]:
    """(driving_s, idle_s) over steps short enough to trust (no holes)."""
    below, max_step = cfg["idle"]["below_kmh"], cfg["distance"]["bridge_max_s"]
    driving = idle = 0.0
    for i in range(1, len(t_s)):
        dt = t_s[i] - t_s[i - 1]
        if 0 < dt <= max_step:
            if v_kmh[i] < below and v_kmh[i - 1] < below:
                idle += dt
            else:
                driving += dt
    return driving, idle


@job(NAME, max_attempts=8)
async def summarise_trip(payload: dict, session: AsyncSession) -> None:
    cfg = settings()
    trip_id = uuid.UUID(payload["trip_id"])
    trip = await session.scalar(select(Trip).where(Trip.id == trip_id).with_for_update())
    if trip is None or trip.status not in (TripStatus.complete, TripStatus.late_data):
        return  # deleted, or still open: assembly queues this stage when the trip closes

    speed_id = catalogue().by_name["speed"].id
    rows = (
        await session.execute(
            select(readings.c.ts, readings.c.value)
            .where(readings.c.trip_id == trip_id, readings.c.signal_id == speed_id)
            .order_by(readings.c.ts)
        )
    ).all()
    t_s = [ts.timestamp() for ts, _ in rows]
    v = [float(val) for _, val in rows]
    fixes = [
        Fix(ts.timestamp(), lat, lon, spd)
        for ts, lat, lon, spd in (
            await session.execute(
                select(positions.c.ts, positions.c.lat, positions.c.lon, positions.c.speed_kmh)
                .where(positions.c.trip_id == trip_id)
                .order_by(positions.c.ts)
            )
        ).all()
    ]

    dist = trip_distance(t_s, v, fixes, cfg)
    driving_s, idle_s = idle_split(t_s, v, cfg)
    brakes, accels = harsh_counts(t_s, v, trip.events, cfg)

    trip.distance_km = round(dist.km, 4)
    trip.distance_source = dist.source if rows else None
    trip.gnss_distance_km = round(dist.gnss_km, 4) if dist.gnss_km is not None else None
    trip.driving_s, trip.idle_s = round(driving_s), round(idle_s)
    trip.idle_share = idle_s / (driving_s + idle_s) if driving_s + idle_s > 0 else None
    trip.max_speed_kmh = max(v) if v else None
    trip.harsh_brake_n, trip.harsh_accel_n = brakes, accels
    trip.summarised_at = datetime.now(UTC)

    await session.execute(_MINUTES, {"trip_id": trip_id})

    deleted = 0
    newest = await session.scalar(
        select(func.max(Trip.ended_at)).where(Trip.vehicle_id == trip.vehicle_id)
    )
    if newest is not None:
        cutoff = newest - timedelta(days=cfg["retention"]["raw_days"])
        for table in (readings, positions):
            res = await session.execute(
                delete(table).where(table.c.vehicle_id == trip.vehicle_id, table.c.ts < cutoff)
            )
            deleted += res.rowcount or 0

    if (nxt := next_stage(NAME)) is not None:
        await enqueue(session, nxt, {"trip_id": str(trip_id)}, dedupe_key=f"{nxt}:{trip_id}")
    log.info(
        "trip summarised",
        extra={"trip_id": str(trip_id), "km": trip.distance_km, "source": trip.distance_source,
               "idle_share": trip.idle_share, "holes": len(dist.holes),
               "raw_rows_retired": deleted},
    )  # fmt: skip

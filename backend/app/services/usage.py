"""How far a car has gone and how fast it's using up its service intervals.

**Odometer estimate** = the newest *trusted* anchor + the distance of every summarised trip
since. Trusted anchors, newest wins:
- `install`: the odometer entered when the device was installed
- `obd_a6`: the car's own odometer (PID 0xA6), when it reports one
- `user` / `service`: an entry by the owner/manager that *agreed* with our estimate
An entry that disagrees by more than max(500 km, 5 % of km since the anchor) is stored and
flagged, but never becomes the anchor: "distance since install is the trusted number"
(PRD 5; odometer tampering is common in imported cars).

**km/day** = km driven in the trailing 30 calendar days / 30 (days parked count as 0).
A car with under 7 days of data blends its own mean with a Lagos prior (30 km/day,
counted as 7 days of data); a car with no data at all uses the prior. (Decisions Q4 and
"30 days", 2026-11-20; numbers from notebook 02, SEED M5/M6.)

The calculations are pure functions; `usage_for()` and `record_odometer()` read and write
the database around them.
"""

from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta, timezone
from functools import lru_cache
from pathlib import Path

import yaml

CONFIG = Path(__file__).resolve().parents[1] / "config" / "usage.yaml"
# West Africa Time: UTC+1 all year (no daylight saving), so a fixed offset is exact and
# needs no time-zone database (Windows and slim images may not have one).
LAGOS = timezone(timedelta(hours=1), "WAT")
TRUSTED_ALWAYS = ("install", "obd_a6")


@lru_cache
def settings() -> dict:
    return yaml.safe_load(CONFIG.read_text(encoding="utf-8"))


@dataclass(frozen=True)
class Anchor:
    km: int
    at: datetime
    source: str  # install / obd_a6 / user / service
    seq: int = -1  # reading id: breaks ties when two readings share a timestamp


@dataclass(frozen=True)
class TripKm:
    started_at: datetime
    km: float
    partial: bool = False  # distance_source == "partial": a lower bound


@dataclass(frozen=True)
class OdometerEstimate:
    km: int | None  # None if no odometer anchor is known yet
    anchor: Anchor | None
    km_since_anchor: float
    partial_trips: int  # trips since the anchor whose distance is a lower bound


@dataclass(frozen=True)
class KmPerDay:
    value: float
    method: str  # mean30 / blend / prior
    days_of_data: float


@dataclass(frozen=True)
class Mismatch:
    entered_km: int
    estimate_km: int
    diff_km: int
    threshold_km: int
    flagged: bool


# ---------------------------------------------------------------- pure calculations


def pick_anchor(readings: list[Anchor]) -> Anchor | None:
    """The newest reading (the caller passes only trusted ones). Readings saved in one
    transaction share `now()`; then the later-saved one (higher id) wins."""
    return max(readings, key=lambda a: (a.at, a.seq)) if readings else None


def odometer_estimate(anchor: Anchor | None, trips: list[TripKm]) -> OdometerEstimate:
    since = [t for t in trips if anchor is None or t.started_at >= anchor.at]
    km = sum(t.km for t in since)
    partial = sum(t.partial for t in since)
    return OdometerEstimate(
        km=round(anchor.km + km) if anchor else None,
        anchor=anchor,
        km_since_anchor=km,
        partial_trips=partial,
    )


def km_per_day(
    trips: list[TripKm], first_data_at: datetime | None, now: datetime, cfg: dict | None = None
) -> KmPerDay:
    c = (cfg or settings())["km_per_day"]
    prior = float(c["lagos_prior"])
    if first_data_at is None or not trips:
        return KmPerDay(prior, "prior", 0.0)
    days = max((now - first_data_at).total_seconds() / 86_400, 0.0)
    window = min(days, float(c["window_days"]))
    since = now - timedelta(days=window)
    km = sum(t.km for t in trips if since <= t.started_at <= now)
    if days < c["prior_weight_days"]:
        w = c["prior_weight_days"]
        return KmPerDay((km + prior * w) / (days + w), "blend", days)
    return KmPerDay(km / window, "mean30", days)


def check_mismatch(
    entered_km: int, estimate: OdometerEstimate, cfg: dict | None = None
) -> Mismatch | None:
    """None if there's no estimate to compare with (no anchor yet)."""
    if estimate.km is None:
        return None
    c = (cfg or settings())["odometer_mismatch"]
    threshold = max(c["floor_km"], c["pct_of_km_since_anchor"] / 100 * estimate.km_since_anchor)
    diff = entered_km - estimate.km
    return Mismatch(entered_km, estimate.km, diff, round(threshold), abs(diff) > threshold)


def install_anchor(km: int | None, installed_on: date | None, fallback: datetime) -> Anchor | None:
    """The vehicle's install odometer as an anchor: the start of the install day, Lagos time
    (or when the vehicle was added, if no install date was given)."""
    if km is None:
        return None
    at = datetime.combine(installed_on, time(), LAGOS) if installed_on else fallback
    return Anchor(km, at.astimezone(UTC), "install")


# ---------------------------------------------------------------- database


@dataclass(frozen=True)
class Usage:
    estimate: OdometerEstimate
    km_per_day: KmPerDay
    trips_counted: int


async def _trips(session, vehicle_id) -> list[TripKm]:
    from sqlalchemy import select

    from app.models import Trip

    rows = (
        await session.execute(
            select(Trip.started_at, Trip.distance_km, Trip.distance_source)
            .where(
                Trip.vehicle_id == vehicle_id,
                Trip.distance_km.is_not(None),
                Trip.started_at.is_not(None),
            )
            .order_by(Trip.started_at)
        )
    ).all()
    return [TripKm(s, float(km), src == "partial") for s, km, src in rows]


async def trusted_anchors(session, vehicle) -> list[Anchor]:
    from sqlalchemy import or_, select

    from app.models import OdometerReading

    rows = (
        await session.scalars(
            select(OdometerReading).where(
                OdometerReading.vehicle_id == vehicle.id,
                or_(
                    OdometerReading.source.in_(TRUSTED_ALWAYS),
                    OdometerReading.mismatch.is_(False),
                ),
            )
        )
    ).all()
    anchors = [Anchor(r.km, r.ts, r.source, r.id) for r in rows]
    inst = install_anchor(vehicle.odometer_at_install_km, vehicle.installed_at, vehicle.created_at)
    if inst is not None:
        anchors.append(inst)
    return anchors


async def usage_for(session, vehicle, now: datetime | None = None) -> Usage:
    now = now or datetime.now(UTC)
    trips = await _trips(session, vehicle.id)
    est = odometer_estimate(pick_anchor(await trusted_anchors(session, vehicle)), trips)
    # Data starts at install (quiet days after install are parked days, not missing ones),
    # or at the first trip if that came earlier (install date entered late or not at all).
    starts = [t.started_at for t in trips[:1]]
    if vehicle.installed_at is not None:
        starts.append(datetime.combine(vehicle.installed_at, time(), LAGOS).astimezone(UTC))
    first = min(starts) if starts and trips else None
    return Usage(est, km_per_day(trips, first, now), len(trips))


async def record_odometer(
    session, vehicle, km: int, source: str, user_id=None, at=None, service_record_id=None
):
    """Store an odometer value with our estimate *as of that moment* and the mismatch
    verdict. `at` back-dates it (e.g. a service done last month): the comparison then
    uses only anchors and trips before `at`, so later driving doesn't look like a
    mismatch. Returns (reading, mismatch-or-None). The caller commits."""
    from app.models import OdometerReading

    trips = await _trips(session, vehicle.id)
    anchors = await trusted_anchors(session, vehicle)
    if at is not None:
        trips = [t for t in trips if t.started_at < at]
        anchors = [a for a in anchors if a.at <= at]
    est = odometer_estimate(pick_anchor(anchors), trips)
    verdict = check_mismatch(km, est) if source not in TRUSTED_ALWAYS else None
    reading = OdometerReading(
        vehicle_id=vehicle.id, km=km, source=source, created_by=user_id,
        estimate_km_at_entry=est.km, mismatch=bool(verdict and verdict.flagged),
        service_record_id=service_record_id, **({"ts": at} if at else {}),
    )  # fmt: skip
    session.add(reading)
    await session.flush()
    return reading, verdict

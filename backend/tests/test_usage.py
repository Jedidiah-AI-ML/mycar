"""Phase 3 step 5 gate: odometer estimate, km/day and the mismatch check.

- the estimate after N trips equals the anchor + their distances
- partial trips are counted and flagged
- the mismatch flag sets and clears at the threshold
- km/day handles a new car, a car parked for two weeks, and a car with no device
"""

from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
import yaml
from sqlalchemy import select

from app.models import OdometerReading, Trip, TripStatus
from app.services.usage import (
    Anchor,
    TripKm,
    check_mismatch,
    install_anchor,
    km_per_day,
    odometer_estimate,
    pick_anchor,
    record_odometer,
    settings,
    usage_for,
)
from tests.test_models import make_user, make_vehicle

NOW = datetime(2026, 11, 21, 12, 0, tzinfo=UTC)
REPO = Path(__file__).resolve().parents[2]


def days_ago(d: float) -> datetime:
    return NOW - timedelta(days=d)


# ---------------------------------------------------------------- config = notebook


def test_config_matches_the_notebook():
    nb = REPO / "ml" / "reports" / "phase3_distance.yaml"
    if not nb.exists():
        pytest.skip("ml/ not present (e.g. inside the API image)")
    d = yaml.safe_load(nb.read_text(encoding="utf-8"))["decisions"]
    c = settings()
    assert c["km_per_day"]["lagos_prior"] == d["km_per_day"]["lagos_prior_km_per_day"]
    assert c["km_per_day"]["prior_weight_days"] == d["km_per_day"]["prior_weight_days"]
    assert d["km_per_day"]["chosen"] == "mean30" and c["km_per_day"]["window_days"] == 30
    for k in ("floor_km", "pct_of_km_since_anchor"):
        assert c["odometer_mismatch"][k] == d["odometer_mismatch"][k], k


# ---------------------------------------------------------------- estimate


def test_estimate_is_anchor_plus_trips_since():
    anchor = Anchor(120_000, days_ago(20), "install")
    trips = [
        TripKm(days_ago(25), 50.0),  # before the anchor: not counted
        TripKm(days_ago(10), 12.4),
        TripKm(days_ago(5), 30.1),
        TripKm(days_ago(1), 7.5),
    ]
    est = odometer_estimate(anchor, trips)
    assert est.km == 120_050  # 120,000 + 12.4 + 30.1 + 7.5 = 120,050.0
    assert est.km_since_anchor == pytest.approx(50.0) and est.partial_trips == 0


def test_partial_trips_are_counted_and_flagged():
    anchor = Anchor(80_000, days_ago(9), "install")
    est = odometer_estimate(anchor, [TripKm(days_ago(3), 20.0, partial=True),
                                     TripKm(days_ago(2), 10.0)])  # fmt: skip
    assert est.km == 80_030 and est.partial_trips == 1


def test_no_anchor_means_no_estimate_but_distance_is_known():
    est = odometer_estimate(None, [TripKm(days_ago(2), 10.0)])
    assert est.km is None and est.km_since_anchor == 10.0
    assert check_mismatch(50_000, est) is None


def test_newest_trusted_anchor_wins():
    a = [Anchor(100_000, days_ago(60), "install"), Anchor(103_000, days_ago(10), "user"),
         Anchor(101_500, days_ago(30), "obd_a6")]  # fmt: skip
    assert pick_anchor(a).km == 103_000
    assert pick_anchor([]) is None
    same_time = [Anchor(100_000, NOW, "user", seq=7), Anchor(100_100, NOW, "user", seq=9)]
    assert pick_anchor(same_time).km == 100_100  # same timestamp: the later-saved wins


def test_install_anchor_is_the_start_of_the_install_day_in_lagos():
    a = install_anchor(95_000, date(2026, 11, 1), NOW)
    assert a.at == datetime(2026, 10, 31, 23, 0, tzinfo=UTC)  # 00:00 WAT = 23:00 UTC
    assert install_anchor(None, date(2026, 11, 1), NOW) is None
    assert install_anchor(95_000, None, NOW).at == NOW


# ---------------------------------------------------------------- mismatch threshold


def test_mismatch_flag_sets_and_clears_at_the_threshold():
    anchor = Anchor(100_000, days_ago(300), "install")
    # 20,000 km since the anchor: threshold = max(500, 5 % x 20,000) = 1,000 km.
    est = odometer_estimate(anchor, [TripKm(days_ago(100), 20_000.0)])
    assert est.km == 120_000
    assert not check_mismatch(121_000, est).flagged  # exactly at the threshold: fine
    assert check_mismatch(121_001, est).flagged
    assert not check_mismatch(119_000, est).flagged
    m = check_mismatch(118_999, est)
    assert m.flagged and m.diff_km == -1_001 and m.threshold_km == 1_000
    # A short distance since the anchor: the 500 km floor applies.
    near = odometer_estimate(anchor, [TripKm(days_ago(5), 2_000.0)])
    assert check_mismatch(102_500, near).threshold_km == 500
    assert check_mismatch(102_501, near).flagged


# ---------------------------------------------------------------- km per day


def test_km_per_day_trailing_30_days_counts_parked_days_as_zero():
    trips = [TripKm(days_ago(d), 30.0) for d in range(1, 31)]  # 30 km a day for 30 days
    assert km_per_day(trips, days_ago(60), NOW).value == pytest.approx(30.0)
    # Parked for the last two weeks: those days count as zero.
    parked = [t for t in trips if t.started_at < days_ago(14)]
    k = km_per_day(parked, days_ago(60), NOW)
    assert k.method == "mean30" and k.value == pytest.approx(16 * 30 / 30)
    # Older history beyond 30 days is ignored.
    old = trips + [TripKm(days_ago(45), 5000.0)]
    assert km_per_day(old, days_ago(60), NOW).value == pytest.approx(30.0)


def test_km_per_day_new_car_blends_with_the_lagos_prior():
    trips = [TripKm(days_ago(2), 80.0), TripKm(days_ago(1), 80.0)]  # a heavy first 3 days
    k = km_per_day(trips, days_ago(3), NOW)
    # (160 km + 30 km/day x 7 days) / (3 + 7) days = 37 km/day
    assert k.method == "blend" and k.value == pytest.approx(37.0)
    # After 7 days the car's own data takes over (mean over the days it has, up to 30).
    week = [TripKm(days_ago(d), 50.0) for d in range(1, 11)]
    k = km_per_day(week, days_ago(10), NOW)
    assert k.method == "mean30" and k.value == pytest.approx(50.0)


def test_km_per_day_no_device_or_no_trips_uses_the_prior():
    assert km_per_day([], None, NOW).method == "prior"
    assert km_per_day([], None, NOW).value == 30.0


# ---------------------------------------------------------------- with the database


async def summarised_trip(session, vehicle_id, started: datetime, km: float, source="speed"):
    session.add(Trip(vehicle_id=vehicle_id, device_boot=0, device_trip=int(started.timestamp()),
                     status=TripStatus.complete, started_at=started,
                     ended_at=started + timedelta(minutes=30), distance_km=km,
                     distance_source=source))  # fmt: skip
    await session.flush()


async def test_usage_for_a_vehicle(session):
    now = datetime.now(UTC)
    v = await make_vehicle(session, await make_user(session), odometer_at_install_km=150_000,
                           installed_at=(now - timedelta(days=40)).date())  # fmt: skip
    for d, km in ((35, 400.0), (20, 25.0), (3, 15.0)):
        await summarised_trip(session, v.id, now - timedelta(days=d), km)
    await summarised_trip(session, v.id, now - timedelta(days=2), 10.0, source="partial")
    u = await usage_for(session, v, now=now)
    assert u.estimate.km == 150_450 and u.estimate.partial_trips == 1
    assert u.estimate.anchor.source == "install"
    assert u.km_per_day.method == "mean30"
    assert u.km_per_day.value == pytest.approx((25 + 15 + 10) / 30)
    assert u.trips_counted == 4


async def test_recording_odometer_values(session):
    now = datetime.now(UTC)
    v = await make_vehicle(session, await make_user(session), odometer_at_install_km=60_000,
                           installed_at=(now - timedelta(days=30)).date())  # fmt: skip
    await summarised_trip(session, v.id, now - timedelta(days=10), 1_000.0)
    # Estimate 61,000. Agreeing entry (within 500 km): stored, trusted, becomes the anchor.
    r, m = await record_odometer(session, v, 61_200, "user")
    assert not m.flagged and r.estimate_km_at_entry == 61_000 and r.mismatch is False
    u = await usage_for(session, v)
    assert u.estimate.anchor.source == "user" and u.estimate.km == 61_200
    # A wildly different entry (tampered dash?): stored and flagged, NOT trusted.
    r2, m2 = await record_odometer(session, v, 45_000, "user")
    assert m2.flagged and r2.mismatch is True
    assert (await usage_for(session, v)).estimate.km == 61_200
    # The car's own odometer (PID 0xA6) is always trusted.
    r3, m3 = await record_odometer(session, v, 61_250, "obd_a6")
    assert m3 is None and r3.mismatch is False
    assert (await usage_for(session, v)).estimate.km == 61_250
    stored = (await session.scalars(select(OdometerReading.mismatch)
                                    .where(OdometerReading.vehicle_id == v.id))).all()  # fmt: skip
    assert sorted(stored) == [False, False, True]


async def test_vehicle_without_odometer_or_trips(session):
    v = await make_vehicle(session, await make_user(session))
    u = await usage_for(session, v)
    assert u.estimate.km is None and u.km_per_day.method == "prior" and u.trips_counted == 0
    r, m = await record_odometer(session, v, 88_000, "user")
    assert m is None and r.mismatch is False  # nothing to compare with: becomes the anchor
    assert (await usage_for(session, v)).estimate.km == 88_000

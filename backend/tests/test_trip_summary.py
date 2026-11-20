"""Phase 3 step 4 gate: trip.summarise (distance, idle, harsh, minute summaries, retention).

- distance within the notebook's error band of the simulator's TRUE distance, including
  trips with a lost batch (with and without GPS)
- a long hole without GPS is reported (partial), not silently invented
- re-running is idempotent; retention deletes only old raw rows and keeps the summaries
- the config matches the notebook's decisions (drift test)
"""

import json
import random
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import yaml
from sqlalchemy import func, insert, select, text, update

from app.gateway.signals import catalogue
from app.jobs.handlers.trip_summary import harsh_counts, idle_split
from app.models import Job, JobStatus, Trip, TripStatus, positions, reading_minutes, readings
from app.services.distance import Fix, detour_factor, haversine_m, settings, trip_distance
from tests.test_jobs import fast_worker
from tests.test_models import make_user, make_vehicle

DATA = Path(__file__).parent / "data"
REPO = Path(__file__).resolve().parents[2]
SIM = json.loads((DATA / "sim_trips.json").read_text(encoding="utf-8"))
CFG = settings()


def arrays(trip: dict, drop_s: tuple[float, float] | None = None, gps: bool = True):
    """Speed (s, km/h) and fixes of a fixture trip, optionally with a time window lost."""
    keep = (lambda t: not (drop_s[0] <= t < drop_s[1])) if drop_s else (lambda t: True)
    sp = [(ms / 1000, v) for ms, v in trip["speed"] if keep(ms / 1000)]
    fx = [Fix(ms / 1000, lat, lon, spd) for ms, lat, lon, spd in trip["fixes"] if keep(ms / 1000)]
    return [t for t, _ in sp], [v for _, v in sp], (fx if gps else [])


def middle_window(trip: dict, seconds: float) -> tuple[float, float]:
    t = [ms / 1000 for ms, v in trip["speed"] if v > 15]  # start the hole while moving
    a = t[len(t) // 2]
    return a, a + seconds


# ---------------------------------------------------------------- config = notebook


def test_config_matches_the_notebook():
    nb = REPO / "ml" / "reports" / "phase3_distance.yaml"
    if not nb.exists():
        pytest.skip("ml/ not present (e.g. inside the API image)")
    d = yaml.safe_load(nb.read_text(encoding="utf-8"))["decisions"]["distance"]
    c = CFG["distance"]
    for key in ("bridge_max_s", "no_gps_bridge_max_s", "partial_hole_s", "gnss_disagree_flag_pct"):
        assert c[key] == d[key], key
    assert {int(k): v for k, v in c["detour_factor_by_hole_s"].items()} == {
        int(k): v for k, v in d["detour_factor_by_hole_s"].items()
    }


# ---------------------------------------------------------------- distance vs truth


@pytest.mark.parametrize("i", range(len(SIM)))
def test_complete_trip_matches_true_distance(i):
    trip = SIM[i]
    t, v, fx = arrays(trip)
    r = trip_distance(t, v, fx)
    assert abs(r.km / trip["true_km"] - 1) < 0.003  # notebook: within ±0.1 % (p5-p95)
    assert r.source == "speed" and not [h for h in r.holes if h.length_s > 10]
    assert r.gnss_km is not None and not r.gnss_disagrees


@pytest.mark.parametrize("i", range(len(SIM)))
def test_lost_batch_with_gps_uses_the_gps_chord(i):
    trip = SIM[i]
    t, v, fx = arrays(trip, drop_s=middle_window(trip, 60))
    r = trip_distance(t, v, fx)
    (hole,) = [h for h in r.holes if h.length_s > 10]
    assert hole.method == "gps" and r.source == "speed+gnss"
    assert abs(r.km / trip["true_km"] - 1) < 0.02


@pytest.mark.parametrize("i", range(len(SIM)))
def test_lost_batch_without_gps_is_bridged_and_reported(i):
    trip = SIM[i]
    t, v, _ = arrays(trip, drop_s=middle_window(trip, 60), gps=False)
    r = trip_distance(t, v, [])
    (hole,) = [h for h in r.holes if h.length_s > 10]
    assert hole.method == "bridge" and r.source == "partial"  # 60 s > 20 s: flagged
    assert abs(r.km / trip["true_km"] - 1) < 0.05


def test_long_hole_without_gps_uses_trip_average_speed():
    trip = max(SIM, key=lambda tr: len(tr["speed"]))
    t, v, _ = arrays(trip, drop_s=middle_window(trip, 300), gps=False)
    r = trip_distance(t, v, [])
    (hole,) = [h for h in r.holes if h.length_s > 10]
    assert hole.method == "average_speed" and r.source == "partial"
    assert hole.km == pytest.approx(
        r.covered_km
        / (sum(b - a for a, b in zip(t, t[1:], strict=False) if b - a <= 10) / 3600)
        * hole.length_s
        / 3600
    )


def test_dropped_single_reads_are_simply_bridged():
    trip = SIM[0]
    t, v, fx = arrays(trip)
    rng = random.Random(1)
    keep = [i for i in range(len(t)) if i in (0, len(t) - 1) or rng.random() > 0.05]
    r = trip_distance([t[i] for i in keep], [v[i] for i in keep], fx)
    assert r.source == "speed" and abs(r.km / trip["true_km"] - 1) < 0.005


# ---------------------------------------------------------------- hand-checked cases


def test_constant_speed_hand_case():
    r = trip_distance(list(range(61)), [36.0] * 61)  # 36 km/h for 60 s = 600 m
    assert r.km == pytest.approx(0.6) and r.source == "speed"


def test_gps_chord_hand_case():
    # 36 km/h along a meridian; a 60 s hole with fixes every 5 s on both sides.
    def lat_at(s):  # 10 m/s north
        return 6.5 + 10 * s / 111_195

    t = [s for s in range(0, 301) if not 100 < s < 160]
    fixes = [Fix(s, lat_at(s), 3.38, 36.0) for s in range(0, 301, 5) if not 100 < s < 160]
    r = trip_distance(t, [36.0] * len(t), fixes)
    (hole,) = r.holes
    chord = haversine_m(lat_at(100), 3.38, lat_at(160), 3.38) / 1000
    assert hole.method == "gps"
    assert hole.km == pytest.approx(
        chord * detour_factor(60, CFG["distance"]["detour_factor_by_hole_s"])
    )
    assert hole.km == pytest.approx(0.6 * 1.035, rel=1e-3)  # straight road: chord = 600 m, x1.035


def test_gnss_disagreement_flag():
    t = list(range(0, 600))
    fixes = [Fix(s, 6.5 + 10 * s / 111_195 * 1.2, 3.38, 36.0) for s in range(0, 600, 5)]
    r = trip_distance(t, [36.0] * len(t), fixes)  # GPS says 20 % further than speed
    assert r.gnss_disagrees


# ---------------------------------------------------------------- idle + harsh


def test_idle_split():
    t = list(range(0, 101))
    v = [0.0] * 40 + [30.0] * 61  # 39 s idle (both ends < 2 km/h), then moving
    driving, idle = idle_split(t, v, CFG)
    assert (driving, idle) == (61.0, 39.0)


def test_harsh_counts_merge_device_and_speed_curve():
    t = list(range(0, 30))
    v = [50.0] * 10 + [36.0] + [36.0] * 19  # -14 km/h in 1 s (0.40 g) at t=10: a hard stop
    dev = [{"type": "harsh", "kind": "brake", "ts": 11_000},  # same stop, seen by the device
           {"type": "harsh", "kind": "brake", "ts": 25_000},  # another, only the device saw
           {"type": "harsh", "kind": "accel", "ts": 5_000}]  # fmt: skip
    assert harsh_counts(t, v, dev, CFG) == (2, 1)
    assert harsh_counts(t, v, [], CFG) == (1, 0)
    gentle = [50.0 - 2 * i for i in range(25)] + [0.0] * 5
    assert harsh_counts(t, gentle, [], CFG) == (0, 0)


# ---------------------------------------------------------------- the stage, in the DB


@dataclass
class Stored:
    id: uuid.UUID  # trip id
    vehicle_id: uuid.UUID


def _ts(ms: float) -> datetime:
    return datetime.fromtimestamp(ms / 1000, UTC)


async def stored_trip(session, sim: dict, ended_offset_days: float = 0, vehicle_id=None):
    """A closed trip with the fixture's speed readings and fixes stored, as ingestion
    would. Ids are returned as plain values (ORM objects expire on commit)."""
    if vehicle_id is None:
        user = await make_user(session, f"{uuid.uuid4().hex[:6]}@x.com")
        vehicle_id = (await make_vehicle(session, user)).id
    end_ms = (datetime.now(UTC) - timedelta(days=ended_offset_days)).timestamp() * 1000
    shift = end_ms - sim["speed"][-1][0]
    tr = Trip(
        vehicle_id=vehicle_id, device_boot=0, device_trip=random.randint(1, 10**6),
        status=TripStatus.complete, started_at=_ts(sim["speed"][0][0] + shift),
        ended_at=_ts(sim["speed"][-1][0] + shift),
    )  # fmt: skip
    session.add(tr)
    await session.flush()
    trip_id = tr.id
    sid, rpm = catalogue().by_name["speed"].id, catalogue().by_name["rpm"].id
    base = {"vehicle_id": vehicle_id, "trip_id": trip_id}
    rows = [
        base | {"ts": _ts(ms + shift), "signal_id": sig, "value": val}
        for ms, s in sim["speed"]
        for sig, val in ((sid, s), (rpm, 800 + 40 * s))
    ]
    await session.execute(insert(readings), rows)
    fixes = [
        base | {"ts": _ts(ms + shift), "lat": lat, "lon": lon, "speed_kmh": spd}
        for ms, lat, lon, spd in sim["fixes"]
    ]
    await session.execute(insert(positions), fixes)
    await session.commit()
    return Stored(trip_id, vehicle_id)


async def run_summarise(session, trip_id) -> None:
    from app.jobs.queue import enqueue

    await enqueue(session, "trip.summarise", {"trip_id": str(trip_id)})
    await session.commit()
    assert await fast_worker("w").run_until_empty() >= 1
    session.expire_all()


async def test_stage_writes_the_summary(session):
    sim = SIM[0]
    tr = await stored_trip(session, sim)
    await run_summarise(session, tr.id)
    t = await session.get(Trip, tr.id)
    assert abs(t.distance_km / sim["true_km"] - 1) < 0.003 and t.distance_source == "speed"
    assert abs(t.gnss_distance_km / t.distance_km - 1) < 0.13
    assert t.driving_s + t.idle_s == pytest.approx(
        (sim["speed"][-1][0] - sim["speed"][0][0]) / 1000, abs=2
    )
    assert 0 <= t.idle_share <= 1 and t.max_speed_kmh == max(s for _, s in sim["speed"])
    assert t.summarised_at is not None
    # Per-minute summaries: counts add up to the readings, and means are right.
    n_raw = await session.scalar(select(func.count()).select_from(readings)
                                 .where(readings.c.trip_id == tr.id))  # fmt: skip
    n_min = await session.scalar(select(func.sum(reading_minutes.c.n))
                                 .where(reading_minutes.c.trip_id == tr.id))  # fmt: skip
    assert n_min == n_raw
    sid = catalogue().by_name["speed"].id
    row = (await session.execute(
        select(reading_minutes).where(reading_minutes.c.trip_id == tr.id,
                                      reading_minutes.c.signal_id == sid)
        .order_by(reading_minutes.c.minute).limit(1))).one()  # fmt: skip
    assert row.min <= row.mean <= row.max


async def test_stage_is_idempotent(session):
    tr = await stored_trip(session, SIM[1])
    await run_summarise(session, tr.id)
    first = await session.get(Trip, tr.id)
    snap = (first.distance_km, first.idle_s, first.harsh_brake_n)
    n_rows = await session.scalar(select(func.count()).select_from(reading_minutes))
    await run_summarise(session, tr.id)
    again = await session.get(Trip, tr.id)
    assert (again.distance_km, again.idle_s, again.harsh_brake_n) == snap
    assert await session.scalar(select(func.count()).select_from(reading_minutes)) == n_rows


async def test_open_trip_is_skipped(session):
    tr = await stored_trip(session, SIM[2])
    await session.execute(update(Trip).where(Trip.id == tr.id).values(status=TripStatus.open))
    await session.commit()
    await run_summarise(session, tr.id)
    assert (await session.get(Trip, tr.id)).summarised_at is None


async def test_retention_keeps_180_days_of_the_cars_own_data(session):
    """Raw rows more than 180 days before the car's newest trip go; summaries stay."""
    old = await stored_trip(session, SIM[0], ended_offset_days=200)
    new = await stored_trip(session, SIM[1], ended_offset_days=10, vehicle_id=old.vehicle_id)
    other_old = await stored_trip(session, SIM[2], ended_offset_days=400)  # another car
    await run_summarise(session, old.id)  # summaries for the old trip, before it ages out
    await run_summarise(session, new.id)  # the newest trip triggers retention for this car

    async def raw(trip_id):
        return await session.scalar(select(func.count()).select_from(readings)
                                    .where(readings.c.trip_id == trip_id))  # fmt: skip

    async def pos(trip_id):
        return await session.scalar(select(func.count()).select_from(positions)
                                    .where(positions.c.trip_id == trip_id))  # fmt: skip

    assert await raw(old.id) == 0 and await pos(old.id) == 0  # 190 days before the newest
    assert await raw(new.id) > 0 and await pos(new.id) > 0
    assert await raw(other_old.id) > 0  # another car's data is never touched
    kept = await session.scalar(select(func.count()).select_from(reading_minutes)
                                .where(reading_minutes.c.trip_id == old.id))  # fmt: skip
    assert kept > 0  # the per-minute history of the old trip survives
    # A car parked for a year loses nothing: retention counts from its own newest trip.
    await run_summarise(session, other_old.id)
    assert await raw(other_old.id) > 0


# ---------------------------------------------------------------- the pipeline


async def test_every_late_batch_re_summarises(client, session, at):  # noqa: F811
    """Closing a trip queues trip.summarise; so does each later batch, even when the status
    stays `late_data` (Phase 3 fix)."""
    from tests.test_ingest import make_rig, post
    from tests.test_trips import REAL_NOW, assemble, trip_batches

    at(REAL_NOW)
    rig = await make_rig(client, session)
    docs = trip_batches(rig, n=5)
    for d in docs[:2] + docs[4:]:  # batches 2 and 3 are late
        await post(client, rig, d)
    await assemble(session)

    async def summarised_n():
        return await session.scalar(select(func.count()).select_from(Job).where(
            Job.name == "trip.summarise", Job.status == JobStatus.succeeded))  # fmt: skip

    assert await summarised_n() == 1
    for late in (docs[2], docs[3]):
        await post(client, rig, late)
        await assemble(session)
    assert await summarised_n() == 3  # once on close, once per late batch
    trip = await session.scalar(select(Trip))
    assert trip.status == TripStatus.late_data and trip.distance_source == "speed"
    await session.execute(text("SELECT 1"))


from tests.test_ingest import at  # noqa: E402, F401 - fixture used above

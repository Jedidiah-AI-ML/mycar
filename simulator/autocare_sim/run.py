"""Simulate days of driving for one car: `simulate("toyota_corolla_1_8_2009", days=7, seed=1)`.

Output is the *true* state of the car, second by second. The device emulation (step 5)
decides what of that the Freematics would actually poll, buffer and upload.
Same profile + seed + start date -> byte-identical output.
"""

import hashlib
import math
import zlib
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import numpy as np

from autocare_sim.config import VehicleProfile, cycles, load_profile
from autocare_sim.cycles import speed_trace
from autocare_sim.physics import CRANK_HZ, CarState, Health, TripOutput, simulate_trip

LAGOS = timezone(timedelta(hours=1), "WAT")  # West Africa Time, no daylight saving
DEFAULT_START = datetime(2026, 1, 5, tzinfo=LAGOS)  # a Monday
COOL_TAU_S = 2400.0  # SEED - validate: parked engine cools toward ambient (~40 min)

# A fault model (step 4) answers: given days and km since the start, how healthy is the car?
HealthFn = Callable[[float, float], Health]


@dataclass
class Trip:
    index: int  # 0, 1, 2 ... for this car
    preset: str
    start: datetime  # engine start, Lagos time
    t_ms: np.ndarray  # epoch ms at 1 Hz
    out: TripOutput
    ambient_c: float
    parked_before_s: float
    health: Health
    odometer_km_end: float
    warmups_since_clear: int
    dist_since_clear_km: float

    @property
    def duration_s(self) -> int:
        return len(self.t_ms)

    @property
    def end(self) -> datetime:
        return self.start + timedelta(seconds=self.duration_s)


@dataclass
class Timeline:
    profile: VehicleProfile
    seed: int
    start: datetime
    days: int
    trips: list[Trip] = field(default_factory=list)

    def fingerprint(self) -> str:
        """SHA-256 over every simulated value: equal seeds must give equal fingerprints."""
        hsh = hashlib.sha256()
        for tr in self.trips:
            hsh.update(tr.t_ms.tobytes())
            for name in sorted(tr.out.signals):
                hsh.update(name.encode())
                hsh.update(tr.out.signals[name].tobytes())
            hsh.update(tr.out.lat.tobytes() + tr.out.lon.tobytes() + tr.out.crank_volts.tobytes())
        return hsh.hexdigest()


def ambient_at(when: datetime, day_offset_c: float) -> float:
    c = cycles()["climate"]
    mid = (c["ambient_min_c"] + c["ambient_max_c"]) / 2
    amp = (c["ambient_max_c"] - c["ambient_min_c"]) / 2
    hour = when.hour + when.minute / 60
    return mid + amp * math.cos(2 * math.pi * (hour - c["peak_hour"]) / 24) + day_offset_c


def _day_plan(rng: np.random.Generator) -> list[tuple[float, str]]:
    """(start hour, preset) for each trip of one day, in time order."""
    sch = cycles()["schedule"]
    counts = {int(k): v for k, v in sch["trips_per_day"].items()}
    n = int(rng.choice(list(counts), p=np.array(list(counts.values())) / sum(counts.values())))
    w = np.array(sch["start_hour_weights"], dtype=float)
    hours = sorted(float(rng.choice(24, p=w / w.sum()) + rng.uniform(0, 1)) for _ in range(n))
    mix = sch["cycle_mix"]
    presets = list(mix)
    pw = np.array([mix[k] for k in presets], dtype=float)
    return [(h, presets[int(rng.choice(len(presets), p=pw / pw.sum()))]) for h in hours]


def simulate(
    profile_id: str,
    days: int,
    seed: int,
    start: datetime = DEFAULT_START,
    health_fn: HealthFn | None = None,
) -> Timeline:
    profile = load_profile(profile_id)
    # crc32, not hash(): Python's str hash changes between runs.
    rng = np.random.default_rng([seed, zlib.crc32(profile_id.encode())])
    loc = cycles()["location"]
    clim = cycles()["climate"]
    state = CarState.new(profile, rng, loc["lat"], loc["lon"])
    tl = Timeline(profile=profile, seed=seed, start=start, days=days)
    health_fn = health_fn or (lambda _days, _km: Health())
    min_gap = timedelta(minutes=cycles()["schedule"]["min_gap_between_trips_min"])

    last_end = start - timedelta(hours=12)
    for d in range(days):
        day0 = start + timedelta(days=d)
        day_offset = float(rng.normal(0, clim["day_to_day_sd_c"]))
        for hour, preset in _day_plan(rng):
            t_start = max(day0 + timedelta(hours=hour), last_end + min_gap)
            t_start = t_start.replace(microsecond=0)
            if t_start >= day0 + timedelta(days=1):
                continue  # pushed past midnight by earlier long trips
            amb = ambient_at(t_start, day_offset)
            parked = (t_start - last_end).total_seconds()
            # Engine off: coolant relaxes toward ambient.
            state.coolant_c = amb + (state.coolant_c - amb) * math.exp(-parked / COOL_TAU_S)
            state.iat_c = amb + 8 * math.exp(-parked / 1800)  # heat-soak right after parking
            if state.fuel_l < 0.15 * profile.fuel_tank_l:
                state.fuel_l = profile.fuel_tank_l * float(rng.uniform(0.7, 1.0))  # refuel
            days_since = (t_start - start).total_seconds() / 86400
            health = health_fn(days_since, state.odometer_km)
            speed = speed_trace(preset, rng)
            out = simulate_trip(profile, speed, amb, clim["baro_kpa"], parked, state, health, rng)
            t0_ms = int(t_start.timestamp() * 1000)
            tr = Trip(
                index=len(tl.trips), preset=preset, start=t_start,
                t_ms=t0_ms + np.arange(len(speed), dtype=np.int64) * 1000, out=out,
                ambient_c=amb, parked_before_s=parked, health=health,
                odometer_km_end=state.odometer_km,
                warmups_since_clear=state.warmups_since_clear,
                dist_since_clear_km=state.dist_since_clear_km,
            )  # fmt: skip
            tl.trips.append(tr)
            last_end = tr.end
    return tl


__all__ = ["CRANK_HZ", "LAGOS", "Timeline", "Trip", "ambient_at", "simulate"]

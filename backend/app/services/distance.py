"""Trip distance from 1 Hz speed, with the gap policy measured in notebook 02.

Pure Python (stdlib + PyYAML), so `ml/notebooks/02_distance_and_usage.ipynb` imports this
very module and checks it reproduces the notebook's numbers (PRD 0.5).

- Between samples up to `bridge_max_s` apart: trapezoid, `(v1 + v2) / 2 × Δt`.
- A longer hole, with GPS fixes near both sides: the straight-line GPS distance across
  the hole × a detour factor for that hole length (roads bend).
- A longer hole without GPS: bridge it up to `no_gps_bridge_max_s`, else the trip's
  average speed × the hole's length. Over `partial_hole_s`, the trip is marked partial.
"""

import bisect
import math
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

import yaml

CONFIG = Path(__file__).resolve().parents[1] / "config" / "trip_summary.yaml"
R_EARTH_M = 6_371_008.8


@lru_cache
def settings() -> dict:
    return yaml.safe_load(CONFIG.read_text(encoding="utf-8"))


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance between two points on an Earth-sized sphere, in metres."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi, dlmb = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2
    return 2 * R_EARTH_M * math.asin(math.sqrt(a))


def detour_factor(hole_s: float, table: dict) -> float:
    """Linear interpolation in the notebook's table; clamped at both ends."""
    xs = sorted(int(k) for k in table)
    ys = [table[k] if k in table else table[str(k)] for k in xs]
    if hole_s <= xs[0]:
        return ys[0]
    if hole_s >= xs[-1]:
        return ys[-1]
    i = bisect.bisect_right(xs, hole_s)
    x0, x1, y0, y1 = xs[i - 1], xs[i], ys[i - 1], ys[i]
    return y0 + (y1 - y0) * (hole_s - x0) / (x1 - x0)


@dataclass(frozen=True)
class Fix:
    t_s: float
    lat: float
    lon: float
    speed_kmh: float | None = None


@dataclass
class Hole:
    from_s: float
    to_s: float
    method: str  # bridge / gps / average_speed
    km: float

    @property
    def length_s(self) -> float:
        return self.to_s - self.from_s


@dataclass
class DistanceResult:
    km: float
    source: str  # speed / speed+gnss / partial (= Trip.distance_source)
    covered_km: float  # integrated where samples were close together
    holes: list[Hole] = field(default_factory=list)
    gnss_km: float | None = None  # independent GPS track distance (cross-check)

    @property
    def gnss_disagrees(self) -> bool:
        if not self.gnss_km or not self.km:
            return False
        limit = settings()["distance"]["gnss_disagree_flag_pct"] / 100
        return abs(self.gnss_km / self.km - 1) > limit


def gnss_track_km(fixes: list[Fix], moving_kmh: float) -> float:
    """Sum of hops between consecutive fixes, counting a hop only if the car was moving at
    either end (a parked car's GPS wanders). Unknown speed counts as moving."""
    total = 0.0
    for a, b in zip(fixes, fixes[1:], strict=False):
        moving = (a.speed_kmh is None or a.speed_kmh > moving_kmh) or (
            b.speed_kmh is None or b.speed_kmh > moving_kmh
        )
        if moving:
            total += haversine_m(a.lat, a.lon, b.lat, b.lon)
    return total / 1000


def _chord_km(fixes: list[Fix], t_a: float, t_b: float, max_gap_s: float) -> float | None:
    """GPS straight line from the last fix at/before t_a to the first at/after t_b, scaled
    to the hole's length. None if no fix is close enough on either side."""
    times = [f.t_s for f in fixes]
    i = bisect.bisect_right(times, t_a) - 1
    j = bisect.bisect_left(times, t_b)
    if i < 0 or j >= len(fixes):
        return None
    before, after = fixes[i], fixes[j]
    if t_a - before.t_s > max_gap_s or after.t_s - t_b > max_gap_s:
        return None
    span = after.t_s - before.t_s
    if span <= 0:
        return None
    chord = haversine_m(before.lat, before.lon, after.lat, after.lon) / 1000
    return chord * (t_b - t_a) / span


def trip_distance(
    t_s: list[float], v_kmh: list[float], fixes: list[Fix] | None = None, cfg: dict | None = None
) -> DistanceResult:
    """Distance of one trip from its speed samples (seconds, km/h), sorted by time."""
    c = (cfg or settings())["distance"]
    fixes = sorted(fixes or [], key=lambda f: f.t_s)
    covered_km, covered_s, holes = 0.0, 0.0, []
    for i in range(1, len(t_s)):
        dt = t_s[i] - t_s[i - 1]
        if dt <= 0:
            continue
        seg = (v_kmh[i] + v_kmh[i - 1]) / 2 / 3600 * dt
        if dt <= c["bridge_max_s"]:
            covered_km += seg
            covered_s += dt
        else:
            holes.append(Hole(t_s[i - 1], t_s[i], "", seg))  # method decided below
    mean_kmh = covered_km / covered_s * 3600 if covered_s > 0 else 0.0
    partial = used_gps = False
    for h in holes:
        chord = _chord_km(fixes, h.from_s, h.to_s, c["gps_fix_max_gap_s"]) if fixes else None
        if chord is not None:
            h.method, h.km = "gps", chord * detour_factor(h.length_s, c["detour_factor_by_hole_s"])
            used_gps = True
        elif h.length_s <= c["no_gps_bridge_max_s"]:
            h.method = "bridge"  # km already = the bridged trapezoid
        else:
            h.method, h.km = "average_speed", mean_kmh / 3600 * h.length_s
        if h.method != "gps" and h.length_s > c["partial_hole_s"]:
            partial = True
    km = covered_km + sum(h.km for h in holes)
    source = "partial" if partial else ("speed+gnss" if used_gps else "speed")
    gnss = gnss_track_km(fixes, c["moving_kmh"]) if len(fixes) >= 2 else None
    return DistanceResult(km=km, source=source, covered_km=covered_km, holes=holes, gnss_km=gnss)

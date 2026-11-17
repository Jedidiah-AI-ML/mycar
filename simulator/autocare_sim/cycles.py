"""Drive cycles: a second-by-second speed trace for one trip.

A trip is a chain of *segments* (stop, crawl, flow, cruise), each with a duration and
a target speed drawn from `profiles/cycles.yaml`. The car doesn't jump to the target:
it accelerates and brakes within comfortable limits, with a little driver noise, the way
a real speed trace looks.
"""

import numpy as np

from autocare_sim.config import calibration, cycles, lognormal_from_pcts

PRESETS = ("lagos_heavy", "urban_mixed", "expressway", "short_trip")


def trip_duration_s(preset: str, rng: np.random.Generator) -> int:
    spec = cycles()["presets"][preset]["duration_min"]
    pcts = calibration()["usage_ved"]["trip_duration_min"] if spec == "calibration" else spec
    mu, sigma = lognormal_from_pcts(pcts)
    minutes = float(np.exp(rng.normal(mu, sigma)))
    lo, hi = pcts["p10"] / 2, pcts["p90"] * 2  # trim extreme tails
    return int(round(np.clip(minutes, lo, hi) * 60))


def speed_trace(preset: str, rng: np.random.Generator, duration_s: int | None = None) -> np.ndarray:
    """km/h at 1 Hz, starting and ending at standstill."""
    spec = cycles()["presets"][preset]
    total = duration_s or trip_duration_s(preset, rng)
    seg_names = list(spec["segments"])
    weights = np.array([spec["segments"][n]["weight"] for n in seg_names], dtype=float)
    weights /= weights.sum()

    targets: list[float] = []
    # Pull away after a short warm-up idle (engine just started).
    targets += [0.0] * int(rng.integers(5, 30))
    while len(targets) < total:
        seg = spec["segments"][seg_names[rng.choice(len(seg_names), p=weights)]]
        dur = int(rng.integers(seg["dur_s"][0], seg["dur_s"][1] + 1))
        lo, hi = seg["speed_kmh"]
        tgt = float(rng.uniform(lo, hi))
        # Speed wanders around the target inside a segment (traffic isn't constant).
        wander = np.cumsum(rng.normal(0, 0.6, dur)) if tgt > 0 else np.zeros(dur)
        wander -= np.linspace(0, wander[-1] if dur else 0, dur)  # end back on target
        targets += list(np.clip(tgt + wander, max(lo - 3, 0), hi + 5) if tgt > 0 else wander)
    targets = targets[:total] + [0.0] * 20  # come to a stop and park

    acc = spec["accel_ms2"] * 3.6  # km/h per second
    dec = spec["decel_ms2"] * 3.6
    v = np.zeros(len(targets))
    for i in range(1, len(targets)):
        dv = np.clip(targets[i] - v[i - 1], -dec, acc)
        v[i] = max(v[i - 1] + dv * rng.uniform(0.7, 1.0), 0.0)
        if targets[i] == 0 and v[i] < 1.5:
            v[i] = 0.0  # come to a proper stop
    v[-5:] = 0.0
    return v

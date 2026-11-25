"""Per-trip health indicators (Phase 5, Layer 3). Plain functions on arrays: the notebooks
import them, and the backend promotes the same code (step 4), so both compute the same numbers.

Input: one trip as {signal name: (t_s, values)}, times in seconds from the trip start, the
way the backend stores readings (catalogue names: rpm, speed, coolant, iat, stft_b1, ...).
Each indicator has a **gate**: the conditions under which its value means something. A trip
that fails the gate gives no value (not zero, not an alarm).

Every threshold here is a SEED (Phase 5 plan, H1-H3); `DEFAULTS` holds them and the
backend reads the same numbers from config/health/indicators.yaml.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

DEFAULTS: dict = {
    # H1: "warm idle" - SEED - validate
    "warm_coolant_c": 80.0,
    "idle_speed_kmh": 0.5,
    "idle_rpm_max": 1100.0,
    "min_window_s": 30,
    # data without a coolant signal (VED): assume warm this long after the start - SEED
    "assume_warm_after_s": 600,
    # H2: cold start and warm-up - SEED - validate
    "cold_start_c": 40.0,
    "warmup_target_c": 80.0,
    "coolant_at_s": 600,  # coolant temperature 10 min after a cold start
    "overheat_c": 105.0,
    # H3: resting voltage needs the battery to have settled - SEED - validate
    "rest_parked_s": 3600,
    # steady cruise for fuel trims - SEED
    "cruise_kmh": (50.0, 110.0),
    "cruise_max_accel_kmh_s": 1.5,
    "cruise_rpm_max": 3500.0,
    # high-RPM share - SEED
    "high_rpm": 4000.0,
    # how long a slow-group sample may stand for the signal (zero-order hold)
    "hold_s": 15.0,
}

# Every indicator, with the signals it needs (any one alternative of a tuple will do).
INDICATORS: dict[str, dict] = {
    "batt_rest_v": {"system": "battery", "unit": "V", "needs": ["crank_v"]},
    "batt_crank_min_v": {"system": "battery", "unit": "V", "needs": ["crank_v"]},
    "charging_v_idle": {"system": "charging", "unit": "V", "needs": [("batt_v", "ecu_voltage")]},
    "warmup_s": {"system": "cooling", "unit": "s", "needs": ["coolant"]},
    "coolant_10min_c": {"system": "cooling", "unit": "degC", "needs": ["coolant"]},
    "coolant_idle_peak_c": {"system": "cooling", "unit": "degC", "needs": ["coolant"]},
    "overheat_s": {"system": "cooling", "unit": "s", "needs": ["coolant"]},
    "trim_idle_b1": {"system": "fuel_air", "unit": "%", "needs": ["stft_b1", "ltft_b1"]},
    "trim_idle_b2": {"system": "fuel_air", "unit": "%", "needs": ["stft_b2", "ltft_b2"]},
    "trim_cruise_b1": {"system": "fuel_air", "unit": "%", "needs": ["stft_b1", "ltft_b1"]},
    "trim_cruise_b2": {"system": "fuel_air", "unit": "%", "needs": ["stft_b2", "ltft_b2"]},
    "ltft_b1": {"system": "fuel_air", "unit": "%", "needs": ["ltft_b1"]},
    "maf_idle_norm": {"system": "airflow", "unit": "g/s per L per krpm", "needs": ["maf"]},
    "maf_vs_map": {"system": "airflow", "unit": "ratio", "needs": ["maf", "map", "iat"]},
    "idle_rpm_std": {"system": "ignition", "unit": "rpm", "needs": []},
    "km_since_clear": {"system": "codes", "unit": "km", "needs": ["dist_since_clear"]},
    "high_rpm_share": {"system": "stress", "unit": "share", "needs": []},
}


@dataclass
class TripInput:
    """One trip's readings plus the little context indicators need."""

    signals: dict[str, tuple[np.ndarray, np.ndarray]]
    displacement_l: float | None = None
    parked_before_s: float | None = None  # since the previous trip ended; None = unknown
    has_coolant: bool | None = None  # None: decide from the signals
    cfg: dict = field(default_factory=lambda: dict(DEFAULTS))


@dataclass
class Value:
    value: float
    n: int  # samples (or seconds) the value rests on


# ---------------------------------------------------------------- helpers


def _sig(tr: TripInput, name: str):
    s = tr.signals.get(name)
    if s is None or len(s[0]) == 0:
        return None
    t, v = np.asarray(s[0], float), np.asarray(s[1], float)
    ok = np.isfinite(v)
    return (t[ok], v[ok]) if ok.any() else None


def _grid(tr: TripInput) -> np.ndarray | None:
    """A 1 Hz time grid covering the trip, from the fast group (rpm, else speed)."""
    for name in ("rpm", "speed"):
        s = _sig(tr, name)
        if s is not None and len(s[0]) >= 2:
            return np.arange(np.floor(s[0][0]), np.ceil(s[0][-1]) + 1.0)
    return None


def _hold(t_grid, s, hold_s) -> np.ndarray:
    """Zero-order hold of a sampled signal onto the grid; NaN where the last sample is older
    than `hold_s` (or before the first sample)."""
    out = np.full(len(t_grid), np.nan)
    if s is None:
        return out
    t, v = s
    i = np.searchsorted(t, t_grid, side="right") - 1
    ok = (i >= 0) & (t_grid - t[np.clip(i, 0, None)] <= hold_s)
    out[ok] = v[i[ok]]
    return out


def _windows(mask: np.ndarray, min_len: int) -> list[tuple[int, int]]:
    """Runs of True at least `min_len` long, as [start, end) index pairs."""
    runs, start = [], None
    for k, m in enumerate(np.r_[mask, False]):
        if m and start is None:
            start = k
        elif not m and start is not None:
            if k - start >= min_len:
                runs.append((start, k))
            start = None
    return runs


@dataclass
class _Ctx:
    t: np.ndarray
    rpm: np.ndarray
    speed: np.ndarray
    coolant: np.ndarray
    warm: np.ndarray
    idle_runs: list[tuple[int, int]]
    cruise: np.ndarray


def _context(tr: TripInput) -> _Ctx | None:
    c = tr.cfg
    t = _grid(tr)
    if t is None:
        return None
    rpm = _hold(t, _sig(tr, "rpm"), 3.0)
    speed = _hold(t, _sig(tr, "speed"), 3.0)
    coolant = _hold(t, _sig(tr, "coolant"), c["hold_s"])
    has_coolant = tr.has_coolant if tr.has_coolant is not None else bool(np.isfinite(coolant).any())
    if has_coolant:
        warm = coolant >= c["warm_coolant_c"]
    else:
        warm = (t - t[0]) >= c["assume_warm_after_s"]
    running = rpm > 300
    idle = warm & running & (speed <= c["idle_speed_kmh"]) & (rpm < c["idle_rpm_max"])
    idle_runs = _windows(idle, c["min_window_s"])
    accel = np.abs(np.gradient(speed, t)) if len(t) > 2 else np.zeros_like(t)
    lo, hi = c["cruise_kmh"]
    cruise = (warm & (speed >= lo) & (speed <= hi) & (accel <= c["cruise_max_accel_kmh_s"])
              & (rpm < c["cruise_rpm_max"]))  # fmt: skip
    return _Ctx(t, rpm, speed, coolant, warm, idle_runs, cruise)


def _in_runs(n: int, runs) -> np.ndarray:
    m = np.zeros(n, bool)
    for a, b in runs:
        m[a:b] = True
    return m


def _median_at(tr, x: _Ctx, names: list[str], mask: np.ndarray, hold: float):
    """Median of the sum of `names` where `mask` holds; samples counted on the grid."""
    vals = np.zeros(len(x.t))
    for name in names:
        s = _sig(tr, name)
        if s is None:
            return None
        vals = vals + _hold(x.t, s, hold)
    vals = vals[mask & np.isfinite(vals)]
    # It must rest on at least a minimum window of fresh (held) samples, so one stale
    # reading can't stand for a whole window.
    if len(vals) < tr.cfg["min_window_s"]:
        return None
    return Value(float(np.median(vals)), int(len(vals)))


# ---------------------------------------------------------------- the indicators


def battery(tr: TripInput) -> dict[str, Value]:
    out = {}
    s = _sig(tr, "crank_v")
    if s is None or len(s[1]) < 10:
        return out
    v = s[1]
    out["batt_crank_min_v"] = Value(float(v.min()), int(len(v)))
    parked = tr.parked_before_s
    if parked is not None and parked >= tr.cfg["rest_parked_s"]:
        out["batt_rest_v"] = Value(float(np.median(v[:4])), 4)  # before the starter engages
    return out


def charging(tr: TripInput, x: _Ctx) -> dict[str, Value]:
    if not x.idle_runs:
        return {}
    mask = _in_runs(len(x.t), x.idle_runs)
    for name in ("batt_v", "ecu_voltage"):
        if _sig(tr, name) is not None:
            r = _median_at(tr, x, [name], mask, tr.cfg["hold_s"])
            return {"charging_v_idle": r} if r else {}
    return {}


def cooling(tr: TripInput, x: _Ctx) -> dict[str, Value]:
    c, out = tr.cfg, {}
    s = _sig(tr, "coolant")
    if s is None:
        return out
    t_c, v_c = s
    start_c = float(v_c[0])
    if start_c < c["cold_start_c"] and t_c[0] - x.t[0] <= 30:
        reached = np.flatnonzero(v_c >= c["warmup_target_c"])
        if len(reached):
            out["warmup_s"] = Value(float(t_c[reached[0]] - t_c[0]), int(reached[0] + 1))
        at = t_c[0] + c["coolant_at_s"]
        if t_c[-1] >= at and (x.rpm[x.t <= at] > 300).mean() > 0.9:
            out["coolant_10min_c"] = Value(float(np.interp(at, t_c, v_c)), int((t_c <= at).sum()))
    if x.idle_runs:
        mask = _in_runs(len(x.t), x.idle_runs)
        vals = x.coolant[mask & np.isfinite(x.coolant)]
        if len(vals):
            out["coolant_idle_peak_c"] = Value(float(vals.max()), int(len(vals)))
    over = np.isfinite(x.coolant) & (x.coolant >= c["overheat_c"])
    out["overheat_s"] = Value(float(over.sum()), int(np.isfinite(x.coolant).sum()))
    return out


def fuel_trims(tr: TripInput, x: _Ctx) -> dict[str, Value]:
    out, hold = {}, tr.cfg["hold_s"]
    idle = _in_runs(len(x.t), x.idle_runs)
    for b in ("b1", "b2"):
        names = [f"stft_{b}", f"ltft_{b}"]
        if x.idle_runs and (r := _median_at(tr, x, names, idle, hold)):
            out[f"trim_idle_{b}"] = r
        if x.cruise.sum() >= tr.cfg["min_window_s"] and (r := _median_at(tr, x, names, x.cruise,
                                                                          hold)):  # fmt: skip
            out[f"trim_cruise_{b}"] = r
    if r := _median_at(tr, x, ["ltft_b1"], x.warm, hold):
        out["ltft_b1"] = r
    return out


def airflow(tr: TripInput, x: _Ctx) -> dict[str, Value]:
    """Two airflow checks.
    - `maf_idle_norm`: MAF at warm idle per litre of displacement per 1,000 rpm. At idle the
      engine breathes about the same each time, so a dirty MAF reads low here.
    - `maf_vs_map`: MAF measured / MAF predicted from manifold pressure (speed-density:
      air mass = MAP / (R x T) x displacement x rpm / 120). MAP doesn't depend on the MAF
      sensor, so the ratio drifts when the MAF does. (Absolute load 0x43 is computed by the
      ECU *from* the MAF, so it can't check the MAF.) The volumetric efficiency is unknown
      and taken as constant: the car's own baseline absorbs it.
    """
    out, hold, disp = {}, tr.cfg["hold_s"], tr.displacement_l
    maf = _sig(tr, "maf")
    if maf is None or not disp:
        return out
    m = _hold(x.t, maf, hold)
    if x.idle_runs:
        mask = _in_runs(len(x.t), x.idle_runs) & np.isfinite(m) & (x.rpm > 300)
        if mask.sum():
            norm = m[mask] / (disp * x.rpm[mask] / 1000.0)
            out["maf_idle_norm"] = Value(float(np.median(norm)), int(mask.sum()))
    mp, iat = _sig(tr, "map"), _sig(tr, "iat")
    if mp is not None and iat is not None:
        p = _hold(x.t, mp, hold) * 1000.0  # Pa
        tk = _hold(x.t, iat, 60.0) + 273.15
        mask = x.warm & np.isfinite(m) & np.isfinite(p) & np.isfinite(tk) & (x.rpm > 600)
        if mask.sum() >= tr.cfg["min_window_s"]:
            rho = p[mask] / (287.05 * tk[mask])  # kg/m3
            pred = rho * (disp / 1000.0) * x.rpm[mask] / 120.0 * 1000.0  # g/s at VE = 1
            out["maf_vs_map"] = Value(float(np.median(m[mask] / pred)), int(mask.sum()))
    return out


def ignition(tr: TripInput, x: _Ctx) -> dict[str, Value]:
    """Idle RPM stability: the spread of RPM inside each warm-idle window (its own mean
    removed, so a window at 750 and one at 800 rpm don't count as rough), pooled."""
    if not x.idle_runs:
        return {}
    devs = [x.rpm[a:b] - np.median(x.rpm[a:b]) for a, b in x.idle_runs]
    d = np.concatenate(devs)
    d = d[np.isfinite(d)]
    return {"idle_rpm_std": Value(float(np.std(d)), int(len(d)))} if len(d) > 10 else {}


def codes(tr: TripInput) -> dict[str, Value]:
    s = _sig(tr, "dist_since_clear")
    return {"km_since_clear": Value(float(s[1][0]), 1)} if s is not None else {}


def stress(tr: TripInput, x: _Ctx) -> dict[str, Value]:
    run = x.rpm > 300
    if run.sum() < 60:
        return {}
    share = float((x.rpm[run] >= tr.cfg["high_rpm"]).mean())
    return {"high_rpm_share": Value(share, int(run.sum()))}


def compute(tr: TripInput) -> dict[str, Value]:
    """Every indicator whose gate passes on this trip."""
    out = battery(tr) | codes(tr)
    x = _context(tr)
    if x is None:
        return out
    for f in (charging, cooling, fuel_trims, airflow, ignition, stress):
        out |= f(tr, x)
    return out

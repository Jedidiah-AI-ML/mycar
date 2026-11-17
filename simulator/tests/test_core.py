"""Phase 2 step 3 gate: the simulated car behaves physically, matches the calibration,
and is reproducible."""

import math

import numpy as np
import pytest
from scipy.optimize import curve_fit

from autocare_sim.config import calibration, catalogue, load_profile, profile_ids
from autocare_sim.cycles import speed_trace, trip_duration_s
from autocare_sim.physics import RHO_AIR, CarState, Health, simulate_trip
from autocare_sim.run import simulate

COROLLA = "toyota_corolla_1_8_2009"


@pytest.fixture(scope="module")
def week():
    return {pid: simulate(pid, days=3, seed=7) for pid in profile_ids()}


def all_trips(week):
    return [tr for tl in week.values() for tr in tl.trips]


# ---------------------------------------------------------------- profiles


def test_five_profiles_load_and_reference_known_signals():
    ids = profile_ids()
    assert len(ids) == 5
    cat = catalogue()
    for pid in ids:
        p = load_profile(pid)
        assert set(p.supported) <= set(cat)
        assert "rpm" in p.supported and "speed" in p.supported
        # V6 engines report both banks; inline-4 engines don't.
        assert ("stft_b2" in p.supported) == (p.cylinders == 6)


# ---------------------------------------------------------------- reproducibility


def test_same_seed_same_output_different_seed_different():
    a = simulate(COROLLA, days=2, seed=42).fingerprint()
    b = simulate(COROLLA, days=2, seed=42).fingerprint()
    c = simulate(COROLLA, days=2, seed=43).fingerprint()
    assert a == b
    assert a != c


# ---------------------------------------------------------------- physics


def test_rpm_matches_gear_ratio_when_locked(week):
    checked = 0
    for tl in week.values():
        p = tl.profile
        for tr in tl.trips:
            s, gear = tr.out.signals, tr.out.extras["gear"]
            v = s["speed"]
            steady = np.abs(np.diff(v, prepend=v[0])) < 0.5
            mask = steady & (v >= p.lockup_kmh + 5) & (gear >= 2)
            for i in np.flatnonzero(mask):
                expected = v[i] * p.rpm_per_kmh(int(gear[i]))
                assert abs(s["rpm"][i] / expected - 1) < 0.03
                checked += 1
    assert checked > 100


def test_maf_follows_airflow_physics(week):
    for tl in week.values():
        p = tl.profile
        for tr in tl.trips:
            s = tr.out.signals
            ok = (s["rpm"] > 800) & (s["abs_load"] > 10)
            predicted = s["abs_load"][ok] / 100 * RHO_AIR * p.displacement_l * s["rpm"][ok] / 120
            ratio = s["maf"][ok] / predicted
            # Healthy MAF: the car's own ratio (calibrated spread ±4 %) plus 1 % noise.
            assert 0.9 < np.median(ratio) < 1.1
            assert np.corrcoef(s["maf"][ok], predicted)[0, 1] > 0.98


def test_coolant_settles_at_operating_temperature(week):
    long_trips = [
        (tl.profile, tr) for tl in week.values() for tr in tl.trips if tr.duration_s > 1800
    ]
    assert long_trips
    for p, tr in long_trips:
        late = tr.out.signals["coolant"][1200:]
        assert abs(np.median(late) - p.operating_c) <= 3


def test_charging_voltage_only_with_engine_running(week):
    for tr in all_trips(week):
        s = tr.out.signals
        assert s["batt_v"].min() > 13.5  # engine running: alternator charging
        assert tr.out.resting_v < 13.0  # engine off: battery alone
        assert tr.out.crank_volts.min() < tr.out.resting_v - 1.5  # cranking dip


def test_values_stay_inside_catalogue_ranges(week):
    cat = catalogue()
    for tr in all_trips(week):
        for name, arr in tr.out.signals.items():
            if name in cat:
                assert arr.min() >= cat[name]["min"] and arr.max() <= cat[name]["max"], name


def test_closed_loop_trims_centre_on_the_cars_offset(week):
    for tr in all_trips(week):
        s, cl = tr.out.signals, tr.out.extras["closed_loop"]
        if cl.sum() > 300:
            assert abs(np.median(s["stft_b1"][cl])) < 3  # healthy: small corrections


def test_warmup_time_constant_matches_calibration():
    p = load_profile(COROLLA)
    rng = np.random.default_rng(3)
    tau_cal = calibration()["thermal_kit"]["warmup_tau_s"]["p50"]
    taus = []
    for _ in range(5):
        st = CarState.new(p, rng, 6.5, 3.4)
        st.coolant_c = 26.0
        speed = speed_trace("urban_mixed", rng, duration_s=1800)
        out = simulate_trip(p, speed, 27.0, 101, 8 * 3600, st, Health(), rng)
        c = out.signals["coolant"]
        t = np.arange(len(c))

        def model(t, tf, tau, t0):
            return tf - (tf - t0) * np.exp(-t / tau)

        (_, tau, _), _ = curve_fit(model, t[:1500], c[:1500], p0=(90, 400, 26))
        taus.append(tau)
    assert abs(np.median(taus) / tau_cal - 1) < 0.35


# ---------------------------------------------------------------- drive cycles


def _cycle_stats(preset, n=200, seed=11):
    rng = np.random.default_rng(seed)
    idle, durations, moving_speed = [], [], []
    for _ in range(n):
        v = speed_trace(preset, rng)
        idle.append(np.mean(v < 2))
        durations.append(len(v) / 60)
        moving_speed.append(v.mean())
    return np.median(idle), np.median(durations), np.median(moving_speed)


def test_urban_cycle_matches_ved_calibration():
    usage = calibration()["usage_ved"]
    idle, dur, _ = _cycle_stats("urban_mixed")
    assert abs(idle - usage["idle_share"]["p50"]) < 0.08
    assert abs(dur / usage["trip_duration_min"]["p50"] - 1) < 0.25


def test_lagos_heavy_cycle_is_heavy_traffic():
    idle, dur, mean_speed = _cycle_stats("lagos_heavy", n=100)
    assert 0.38 <= idle <= 0.55  # SEED P2: 40-50 % standing
    assert 9 <= mean_speed <= 18  # SEED P2: 12-18 km/h
    assert dur > 25


def test_trip_durations_are_positive_and_bounded():
    rng = np.random.default_rng(5)
    for preset in ("lagos_heavy", "urban_mixed", "expressway", "short_trip"):
        d = [trip_duration_s(preset, rng) for _ in range(200)]
        assert min(d) > 60 and max(d) < 4 * 3600


# ---------------------------------------------------------------- schedule + health hook


def test_schedule_and_health_hook(week):
    seen = []

    def health_fn(days, km):
        seen.append((days, km))
        return Health(charging_v=13.6 if days > 1.5 else 14.1)

    tl = simulate(COROLLA, days=3, seed=7, health_fn=health_fn)
    assert len(seen) == len(tl.trips) > 3
    assert all(b[0] >= a[0] and b[1] >= a[1] for a, b in zip(seen, seen[1:], strict=False))
    early = [tr for tr in tl.trips if tr.health.charging_v == 14.1]
    late = [tr for tr in tl.trips if tr.health.charging_v == 13.6]
    assert early and late
    assert (
        np.median(late[-1].out.signals["batt_v"]) < np.median(early[0].out.signals["batt_v"]) - 0.3
    )
    # Trips never overlap and start inside their day.
    for a, b in zip(tl.trips, tl.trips[1:], strict=False):
        assert b.start >= a.end
    assert all(not math.isnan(tr.ambient_c) and 20 < tr.ambient_c < 38 for tr in tl.trips)

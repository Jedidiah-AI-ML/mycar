"""Indicator functions on hand-built traces whose right answer is known (Phase 5 step 1)."""

import numpy as np
import pytest

from ml.health.indicators import DEFAULTS, TripInput, compute


def trip(seconds=1200, *, coolant=None, speed=0.0, rpm=750.0, **extra):
    t = np.arange(seconds, dtype=float)
    sig = {
        "rpm": (t, np.full(seconds, rpm) if np.isscalar(rpm) else rpm),
        "speed": (t, np.full(seconds, speed) if np.isscalar(speed) else speed),
    }
    if coolant is not None:
        tc = t[::5]
        cv = np.full(len(tc), coolant) if np.isscalar(coolant) else np.asarray(coolant)[::5]
        sig["coolant"] = (tc, cv)
    for k, v in extra.items():
        sig[k] = v
    return sig


def slow(seconds, value):
    t = np.arange(0, seconds, 5, dtype=float)
    return (t, np.full(len(t), value))


def test_warm_idle_trims_are_stft_plus_ltft():
    s = trip(coolant=88.0, stft_b1=slow(1200, 2.5), ltft_b1=slow(1200, 4.0))
    v = compute(TripInput(s, displacement_l=1.8))
    assert v["trim_idle_b1"].value == pytest.approx(6.5)
    assert v["ltft_b1"].value == pytest.approx(4.0)
    assert "trim_cruise_b1" not in v  # never cruised


def test_a_cold_engine_gives_no_trim_or_idle_values():
    s = trip(coolant=50.0, stft_b1=slow(1200, 2.5), ltft_b1=slow(1200, 4.0))
    v = compute(TripInput(s, displacement_l=1.8))
    assert not {"trim_idle_b1", "idle_rpm_std", "coolant_idle_peak_c"} & set(v)


def test_without_coolant_warm_means_after_ten_minutes():
    s = trip(seconds=900, stft_b1=slow(900, 1.0), ltft_b1=slow(900, 1.0))
    v = compute(TripInput(s, displacement_l=2.0, has_coolant=False))
    assert v["trim_idle_b1"].n == 300  # only the last 300 s count as warm


def test_cruise_trims_need_steady_cruise():
    speed = np.r_[np.zeros(100), np.full(600, 80.0), np.zeros(100)]
    rpm = np.r_[np.full(100, 750.0), np.full(600, 2200.0), np.full(100, 750.0)]
    s = trip(800, coolant=88.0, speed=speed, rpm=rpm,
             stft_b1=slow(800, -1.0), ltft_b1=slow(800, 3.0))  # fmt: skip
    v = compute(TripInput(s, displacement_l=1.8))
    assert v["trim_cruise_b1"].value == pytest.approx(2.0)
    assert v["trim_cruise_b1"].n >= 550


def test_warmup_time_and_coolant_at_ten_minutes():
    cool = np.minimum(30.0 + np.arange(1500) * 0.06, 90.0)  # reaches 80 at 833 s
    v = compute(TripInput(trip(1500, coolant=cool), displacement_l=1.8))
    assert v["warmup_s"].value == pytest.approx(835, abs=5)  # 5 s sampling
    assert v["coolant_10min_c"].value == pytest.approx(66.0, abs=0.5)


def test_a_warm_start_has_no_warmup_value():
    v = compute(TripInput(trip(coolant=85.0), displacement_l=1.8))
    assert "warmup_s" not in v and "coolant_10min_c" not in v


def test_overheat_seconds():
    cool = np.r_[np.full(600, 95.0), np.full(120, 108.0), np.full(480, 96.0)]
    v = compute(TripInput(trip(coolant=cool), displacement_l=1.8))
    assert v["overheat_s"].value == pytest.approx(120, abs=5)


def test_resting_voltage_needs_an_hour_parked():
    crank = (np.arange(60) / 50.0, np.r_[np.full(5, 12.6), np.full(50, 9.5), np.full(5, 13.9)])
    s = trip(coolant=85.0, crank_v=crank)
    v = compute(TripInput(s, displacement_l=1.8, parked_before_s=7200))
    assert v["batt_rest_v"].value == pytest.approx(12.6)
    assert v["batt_crank_min_v"].value == pytest.approx(9.5)
    v = compute(TripInput(s, displacement_l=1.8, parked_before_s=600))
    assert "batt_rest_v" not in v and "batt_crank_min_v" in v


def test_charging_voltage_prefers_the_device_reading():
    s = trip(coolant=88.0, batt_v=slow(1200, 14.1), ecu_voltage=slow(1200, 13.8))
    assert compute(TripInput(s))["charging_v_idle"].value == pytest.approx(14.1)
    del s["batt_v"]
    assert compute(TripInput(s))["charging_v_idle"].value == pytest.approx(13.8)


def test_maf_vs_map_is_one_for_speed_density_air():
    rpm, map_kpa, iat_c, disp = 2000.0, 60.0, 30.0, 1.8
    rho = map_kpa * 1000 / (287.05 * (iat_c + 273.15))
    maf = rho * disp / 1000 * rpm / 120 * 1000
    s = trip(coolant=88.0, rpm=rpm, speed=60.0, maf=slow(1200, maf), map=slow(1200, map_kpa),
             iat=slow(1200, iat_c))  # fmt: skip
    v = compute(TripInput(s, displacement_l=disp))
    assert v["maf_vs_map"].value == pytest.approx(1.0, abs=1e-9)
    s["maf"] = slow(1200, maf * 0.8)  # a dirty MAF reads 20 % low
    assert compute(TripInput(s, displacement_l=disp))["maf_vs_map"].value == pytest.approx(0.8)


def test_maf_idle_norm():
    s = trip(coolant=88.0, rpm=800.0, maf=slow(1200, 2.88))
    v = compute(TripInput(s, displacement_l=1.8))
    assert v["maf_idle_norm"].value == pytest.approx(2.88 / (1.8 * 0.8))


def test_idle_rpm_spread_ignores_each_window_s_own_level():
    rng = np.random.default_rng(1)
    calm = np.r_[np.full(300, 700.0), np.full(300, 3000.0), np.full(300, 800.0)]
    speed = np.r_[np.zeros(300), np.full(300, 60.0), np.zeros(300)]
    v = compute(TripInput(trip(900, coolant=88.0, rpm=calm, speed=speed)))
    assert v["idle_rpm_std"].value == pytest.approx(0.0)  # 700 vs 800 is not roughness
    rough = calm + rng.normal(0, 25, 900)
    v = compute(TripInput(trip(900, coolant=88.0, rpm=rough, speed=speed)))
    assert v["idle_rpm_std"].value == pytest.approx(25, rel=0.15)


def test_stale_samples_do_not_stand_for_the_signal():
    s = trip(coolant=88.0, stft_b1=(np.array([0.0]), np.array([5.0])),
             ltft_b1=(np.array([0.0]), np.array([0.0])))  # fmt: skip
    v = compute(TripInput(s))
    assert "trim_idle_b1" not in v  # one sample at t=0 can't stand for 20 minutes


def test_defaults_are_the_documented_seeds():
    assert DEFAULTS["warm_coolant_c"] == 80.0 and DEFAULTS["rest_parked_s"] == 3600
    assert DEFAULTS["min_window_s"] == 30 and DEFAULTS["cold_start_c"] == 40.0

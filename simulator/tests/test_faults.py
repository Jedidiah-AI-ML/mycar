"""Phase 2 step 4 gate: faults bend the right signals (and only their own system), a
clean car sets no DTCs, and the ground truth matches the DTC events."""

import json

import numpy as np
import pytest

from autocare_sim import dtc
from autocare_sim.config import load_profile, profile_ids
from autocare_sim.cycles import speed_trace
from autocare_sim.dtc import Ecu, metric, rules
from autocare_sim.faults import fault, health_from, library
from autocare_sim.physics import CarState, Health, simulate_trip
from autocare_sim.run import fault_codes, simulate

COROLLA = "toyota_corolla_1_8_2009"
RX350 = "lexus_rx350_3_5_2010"  # V6: has bank 2


# ---------------------------------------------------------------- library


def test_every_fault_has_a_dtc_rule_or_is_declared_undiagnosed():
    lib, r = library(), rules()
    caused = {f for spec in r["monitors"].values() for f in spec["caused_by"]}
    assert len(lib) == 9
    assert caused <= set(lib)
    assert set(lib) == caused | set(r["no_dtc"])


def test_severity_curve():
    f = fault("vacuum_leak", onset=5, ramp=10, shape=2.0)
    assert f.severity(4.9, 0) == 0
    assert f.severity(10, 0) == pytest.approx(0.25)  # halfway, shape 2 -> 0.25
    assert f.severity(15, 0) == 1 and f.severity(99, 0) == 1
    s = [f.severity(d, 0) for d in np.linspace(0, 20, 50)]
    assert all(b >= a for a, b in zip(s, s[1:], strict=False))
    km = fault("dirty_maf", onset=100, ramp=200)
    assert km.severity(999, 99) == 0 and km.severity(0, 300) == 1  # km clock ignores days


def test_health_moves_from_healthy_to_end_value():
    f = fault("failing_alternator")
    assert f.apply(Health(), 0.0) == Health()
    assert f.apply(Health(), 1.0).charging_v == pytest.approx(f.end["charging_v"])
    half = f.apply(Health(), 0.5).charging_v
    assert half == pytest.approx((Health().charging_v + f.end["charging_v"]) / 2)
    both = health_from([f, fault("misfire", cylinder=3)])(999, 99_999)
    assert both.charging_v < 13 and both.misfire_rate > 0 and both.misfire_cylinder == 3


# ---------------------------------------------------------------- symptoms, one trip

# What each fully-failed fault must do to the ECU's monitor metrics, vs a clean car on
# the identical trip (same speed trace, same random stream).
UP, DOWN = 1, -1
EXPECT = {
    "weak_battery": {"resting_v": DOWN, "crank_min": DOWN},
    "failing_alternator": {"system_voltage": DOWN},
    "stuck_open_thermostat": {"thermostat": DOWN, "overheat": DOWN},
    "low_coolant": {"thermostat": UP, "overheat": UP},
    "vacuum_leak": {"fuel_system_b1": UP, "fuel_system_b2": UP},
    "dirty_maf": {"maf": DOWN, "fuel_system_b1": UP, "fuel_system_b2": UP},
    "ageing_o2": {"o2_sensor": DOWN},
    "misfire": {"misfire": UP, "o2_sensor": UP},  # unburnt O2 reads lean: real side effect
    "catalyst_degradation": {"catalyst": UP},
}
# A cold engine never reaches the warm closed-loop conditions those monitors need: the
# monitors don't run (as on a real car with a stuck-open thermostat).
MAY_NOT_RUN = {"stuck_open_thermostat": {"fuel_system_b1", "fuel_system_b2", "maf", "o2_sensor",
                                         "catalyst"}}  # fmt: skip
# Unrelated metrics may drift a little (e.g. a hotter engine shifts which seconds count as
# "warm closed loop"); the fault's own effects are many times larger than this.
ABS_TOL = {"fuel_system_b1": 1.0, "fuel_system_b2": 1.0, "misfire": 0.01}
MIN_MOVE = {"fuel_system_b1": 10.0, "fuel_system_b2": 10.0, "misfire": 1.0}  # else 3 %


def _trip_metrics(pid: str, h: Health) -> dict[str, float | None]:
    p = load_profile(pid)
    rng = np.random.default_rng(5)
    st = CarState.new(p, rng, 6.5, 3.4)
    st.coolant_c = 28.0
    speed = np.concatenate([
        speed_trace("urban_mixed", rng, duration_s=1500), np.zeros(240),
        speed_trace("expressway", rng, duration_s=900)])  # fmt: skip
    out = simulate_trip(p, speed, 30.0, 101, 8 * 3600, st, h, np.random.default_rng(105))
    m = {mon: metric(spec["metric"], spec, p, out) for mon, spec in rules()["monitors"].items()}
    m["resting_v"], m["crank_min"] = out.resting_v, float(out.crank_volts.min())
    return m


@pytest.fixture(scope="module")
def clean_trip():
    return {pid: _trip_metrics(pid, Health()) for pid in (COROLLA, RX350)}


@pytest.mark.parametrize("pid", [COROLLA, RX350])
@pytest.mark.parametrize("name", sorted(EXPECT))
def test_fault_bends_its_signals_and_only_its_system(name, pid, clean_trip):
    base = clean_trip[pid]
    f = fault(name)
    got = _trip_metrics(pid, f.apply(Health(), 1.0))
    for key, direction in EXPECT[name].items():
        if base[key] is None:
            assert key.endswith("_b2") and load_profile(pid).cylinders == 4
            continue
        change = got[key] - base[key]
        need = MIN_MOVE.get(key, 0.03 * abs(base[key]))
        assert change * direction > need, (key, base[key], got[key])
    for key in base.keys() - EXPECT[name].keys():
        if got[key] is None and key in MAY_NOT_RUN.get(name, ()):
            continue
        assert (got[key] is None) == (base[key] is None), key
        if base[key] is not None:
            tol = ABS_TOL.get(key, 0.10 * abs(base[key]))
            assert abs(got[key] - base[key]) <= tol, (key, base[key], got[key])


# ---------------------------------------------------------------- clean cars, 30 days


@pytest.fixture(scope="module")
def clean_month():
    return {pid: simulate(pid, days=30, seed=1) for pid in profile_ids()}


def test_clean_cars_set_no_dtcs_over_30_days(clean_month):
    for pid, tl in clean_month.items():
        assert tl.dtc_events() == [], pid
        last = tl.trips[-1].diag
        assert not last.mil and last.pending == [] and last.confirmed == []
        # Every monitor that applies to the car got a chance to run (readiness complete).
        expected = {
            m
            for m, s in rules()["monitors"].items()
            if s.get("enable", {}).get("needs_signal", "rpm") in tl.profile.supported
        }
        assert set(last.monitors_complete) == expected, pid  # fmt: skip


# ---------------------------------------------------------------- DTCs per fault

# Faster than the library defaults so each runs in 3 simulated weeks.
FAST = {"days": {"onset": 1, "ramp": 8}, "km": {"onset": 20, "ramp": 300}}


def _fast(name: str, **kw):
    return fault(name, **FAST[library()[name]["clock"]], **kw)


@pytest.mark.parametrize("name", sorted(library()))
def test_fault_sets_its_codes_pending_then_confirmed(name):
    pid = RX350 if name in ("vacuum_leak", "dirty_maf") else COROLLA
    f = _fast(name)
    tl = simulate(pid, days=21, seed=3, faults=[f])
    gt = tl.ground_truth()
    expected = set(fault_codes(f, tl.profile))
    emitted = {e["code"] for e in gt["dtc_events"]}
    assert emitted == expected  # its own codes, nothing else
    row = gt["faults"][0]
    assert row["start"] is not None
    for code, when in row["codes"].items():
        pend, conf = when["pending"], when["confirmed"]
        assert pend and conf, code
        assert row["start"]["trip"] <= pend["trip"] < conf["trip"]
        assert pend["km"] <= conf["km"]
    if not expected:  # weak battery: no DTC, but the symptom is there
        assert tl.trips[-1].out.resting_v < tl.trips[0].out.resting_v - 0.4
    else:
        assert tl.trips[-1].diag.mil


def test_misfire_code_names_the_cylinder():
    tl = simulate(COROLLA, days=21, seed=3, faults=[_fast("misfire", cylinder=4)])
    assert {e["code"] for e in tl.dtc_events()} == {"P0304"}


# ---------------------------------------------------------------- ground truth


def test_ground_truth_matches_dtc_events(tmp_path):
    faults = [_fast("vacuum_leak"), _fast("catalyst_degradation")]
    tl = simulate(RX350, days=21, seed=4, faults=faults)
    path = tmp_path / "gt.json"
    tl.write_ground_truth(path)
    gt = json.loads(path.read_text(encoding="utf-8"))
    assert gt["car"] == RX350 and len(gt["faults"]) == 2

    # Each fault's pending/confirmed entry is the first matching event.
    for row in gt["faults"]:
        for code, when in row["codes"].items():
            for status in ("pending", "confirmed"):
                ev = gt["dtc_events"]
                first = next(e for e in ev if (e["code"], e["status"]) == (code, status))
                assert when[status] == {k: first[k] for k in ("trip", "days", "km")}

    # Replaying the events reproduces the ECU state reported after every trip.
    pending, confirmed = set(), set()
    by_trip: dict[int, list] = {}
    for e in gt["dtc_events"]:
        by_trip.setdefault(e["trip"], []).append(e)
    for tr in tl.trips:
        for e in by_trip.get(tr.index, []):
            if e["status"] == "pending":
                pending.add(e["code"])
            elif e["status"] == "pending_cleared":
                pending.discard(e["code"])
            elif e["status"] == "confirmed":
                pending.discard(e["code"])
                confirmed.add(e["code"])
            elif e["status"] == "erased":
                confirmed.discard(e["code"])
        assert sorted(pending) == tr.diag.pending and sorted(confirmed) == tr.diag.confirmed


def test_same_seed_same_ground_truth():
    f = [_fast("ageing_o2")]
    a = simulate(COROLLA, days=10, seed=9, faults=f).ground_truth()
    b = simulate(COROLLA, days=10, seed=9, faults=f).ground_truth()
    assert a == b


# ---------------------------------------------------------------- two-trip logic


def test_two_trip_logic_mil_off_and_erase(monkeypatch):
    """Scripted monitor results: fail, pass, fail, fail, not run, 3 passes, 40 warm-ups."""
    script: list[float | None] = []

    def fake_metric(name, spec, p, out):
        return script[0] if name == "running_voltage" else None

    monkeypatch.setattr(dtc, "metric", fake_metric)
    ecu = Ecu(load_profile(COROLLA))
    statuses = []

    def trip(v, warmup=True):
        script[:] = [v]
        d = ecu.after_trip(len(statuses), None, Health(), warmup)
        statuses.append([t.status for t in d.transitions])
        return d

    assert trip(12.0).pending == ["P0562"]  # fail -> pending
    assert trip(14.1).pending == []  # pass -> pending cleared
    trip(12.0)
    d = trip(12.0)  # two consecutive fails -> confirmed + MIL
    assert d.confirmed == ["P0562"] and d.mil
    assert trip(None).mil  # monitor didn't run: nothing changes
    assert ecu.mil_codes == {"P0562"}
    for _ in range(2):
        assert trip(14.1).mil
    assert not trip(14.1).mil  # third pass -> MIL off, code still stored
    assert ecu.confirmed
    for _ in range(40):
        d = trip(14.1)
    assert d.confirmed == []  # erased after 40 warm-ups without a failure
    flat = [s for st in statuses for s in st]
    assert flat == ["pending", "pending_cleared", "pending", "confirmed", "mil_off", "erased"]

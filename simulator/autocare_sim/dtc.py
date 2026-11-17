"""The simulated ECU's on-board diagnostics: monitors, DTCs and the MIL.

After every trip, each monitor in `faults/dtc_rules.yaml` looks at that trip's signals the
way an ECU would (e.g. "were the fuel trims above +25 % at warm idle?"). If its enabling
conditions weren't met it doesn't run. The results feed generic OBD-II two-trip logic:
first failure -> pending, second consecutive failure -> confirmed + MIL.

The DTC timeline is *derived from the signals*, not from the fault labels, so a fault
that doesn't show strongly enough on a given trip doesn't set its code yet: like a car.
"""

from dataclasses import dataclass, field
from functools import lru_cache

import numpy as np
import yaml

from autocare_sim.config import VehicleProfile
from autocare_sim.faults import FAULTS_DIR
from autocare_sim.physics import RHO_AIR, Health, TripOutput


@lru_cache
def rules() -> dict:
    return yaml.safe_load((FAULTS_DIR / "dtc_rules.yaml").read_text(encoding="utf-8"))


# ---------------------------------------------------------------- monitor metrics
# Each returns the monitor's metric for one trip, or None if it didn't run.


def _warm_cl(out: TripOutput, min_coolant_c: float) -> np.ndarray:
    return out.extras["closed_loop"] & (out.signals["coolant"] >= min_coolant_c)


def metric(name: str, spec: dict, p: VehicleProfile, out: TripOutput) -> float | None:
    s, en = out.signals, spec.get("enable", {})
    n = len(s["speed"])
    if "needs_signal" in en and en["needs_signal"] not in p.supported:
        return None
    if n < en.get("min_duration_s", 0):
        return None

    if name == "coolant_after_warmup":
        if out.extras["coolant_start_c"] > p.operating_c - en["cold_start_below_operating_c"]:
            return None
        return float(s["coolant"][en["at_s"] - 1])

    if name == "coolant_max":
        return float(s["coolant"].max())

    if name in ("idle_total_trim_b1", "idle_total_trim_b2"):
        b = name[-2:]
        m = _warm_cl(out, en["min_coolant_c"]) & (s["speed"] < 1)
        if m.sum() < en["min_samples"]:
            return None
        return float(np.median(s[f"stft_{b}"][m] + s[f"ltft_{b}"][m]))

    if name == "maf_rationality":
        m = _warm_cl(out, en["min_coolant_c"]) & (s["rpm"] > 800) & (s["abs_load"] > 10)
        if m.sum() < en["min_samples"]:
            return None
        expected = s["abs_load"][m] / 100 * RHO_AIR * p.displacement_l * s["rpm"][m] / 120
        return float(np.median(s["maf"][m] / expected))

    if name == "o2_activity":
        m = _warm_cl(out, en["min_coolant_c"])
        m = m[1:] & m[:-1]
        if m.sum() < en["min_samples"]:
            return None
        return float(np.median(np.abs(np.diff(s["lambda_b1s1"]))[m]))

    if name == "misfires_per_1000_rev":
        revs = float(s["rpm"].sum() / 60)
        if revs < en["min_revs"]:
            return None
        return out.extras["misfire_events"] / revs * 1000

    if name == "catalyst_downstream_sd":
        m = _warm_cl(out, en["min_coolant_c"])
        if m.sum() < en["min_samples"]:
            return None
        return float(np.std(s["o2_b1s2_v"][m]))

    if name == "running_voltage":
        return float(np.median(s["batt_v"]))

    raise ValueError(f"unknown monitor metric {name}")


def failed(spec: dict, value: float, p: VehicleProfile) -> bool:
    if "fail_below_operating_c" in spec:
        return value < p.operating_c - spec["fail_below_operating_c"]
    if "fail_above_c" in spec:
        return value > spec["fail_above_c"]
    if "fail_above_pct" in spec:
        return value > spec["fail_above_pct"]
    if "fail_outside" in spec:
        lo, hi = spec["fail_outside"]
        return not lo <= value <= hi
    if "fail_below" in spec:
        return value < spec["fail_below"]
    if "fail_above" in spec:
        return value > spec["fail_above"]
    if "fail_below_v" in spec:
        return value < spec["fail_below_v"]
    raise ValueError(f"monitor has no fail rule: {spec}")


def codes_for(spec: dict, health: Health) -> list[str]:
    if spec.get("per_cylinder") and health.misfire_cylinder:
        return [f"P030{health.misfire_cylinder}"]
    return list(spec["codes"])


# ---------------------------------------------------------------- DTC state machine


@dataclass
class Transition:
    code: str
    status: str  # pending | confirmed | pending_cleared | mil_off | erased
    trip: int
    monitor: str


@dataclass
class TripDiagnostics:
    """The ECU's diagnostic state at the end of a trip (what Modes 01/03/07 would read)."""

    results: dict[str, bool | None]  # monitor -> failed? (None = didn't run)
    values: dict[str, float | None]  # monitor -> metric value
    pending: list[str]
    confirmed: list[str]
    mil: bool
    monitors_complete: list[str]  # monitors that have run since codes were cleared
    transitions: list[Transition]


@dataclass
class Ecu:
    profile: VehicleProfile
    pending: dict[str, str] = field(default_factory=dict)  # code -> monitor
    confirmed: dict[str, str] = field(default_factory=dict)
    mil_codes: set[str] = field(default_factory=set)
    passes: dict[str, int] = field(default_factory=dict)  # MIL-on code -> passing runs
    clean_warmups: dict[str, int] = field(default_factory=dict)  # confirmed code -> warm-ups
    complete: set[str] = field(default_factory=set)

    def after_trip(self, trip: int, out: TripOutput, health: Health, warmup: bool):
        logic = rules()["logic"]
        if logic["trips_to_confirm"] != 2:
            raise NotImplementedError("only two-trip confirmation is modelled")
        results, values, tr = {}, {}, []
        for mon, spec in rules()["monitors"].items():
            v = metric(spec["metric"], spec, self.profile, out)
            values[mon] = v
            if v is None:
                results[mon] = None
                continue
            self.complete.add(mon)
            bad = failed(spec, v, self.profile)
            results[mon] = bad
            # Codes this monitor can report (a misfire on cylinder 2 reports P0302).
            mine = {c for c, m in {**self.pending, **self.confirmed}.items() if m == mon}
            if bad:
                for code in codes_for(spec, health):
                    if code in self.pending:
                        del self.pending[code]
                        self.confirmed[code] = mon
                        self.mil_codes.add(code)
                        self.passes[code] = 0
                        self.clean_warmups[code] = 0
                        tr.append(Transition(code, "confirmed", trip, mon))
                    elif code in self.confirmed:
                        self.mil_codes.add(code)
                        self.passes[code] = 0
                        self.clean_warmups[code] = 0
                    else:
                        self.pending[code] = mon
                        tr.append(Transition(code, "pending", trip, mon))
            else:
                for code in mine:
                    if code in self.pending:
                        del self.pending[code]
                        tr.append(Transition(code, "pending_cleared", trip, mon))
                    elif code in self.mil_codes:
                        self.passes[code] += 1
                        if self.passes[code] >= logic["passes_to_mil_off"]:
                            self.mil_codes.discard(code)
                            tr.append(Transition(code, "mil_off", trip, mon))
        if warmup:
            for code in list(self.confirmed):
                if results.get(self.confirmed[code]) is not True:
                    self.clean_warmups[code] += 1
                    if self.clean_warmups[code] >= logic["warmups_to_erase"]:
                        mon = self.confirmed.pop(code)
                        self.mil_codes.discard(code)
                        tr.append(Transition(code, "erased", trip, mon))
        return TripDiagnostics(
            results=results,
            values=values,
            pending=sorted(self.pending),
            confirmed=sorted(self.confirmed),
            mil=bool(self.mil_codes),
            monitors_complete=sorted(self.complete),
            transitions=tr,
        )

"""Loads vehicle profiles, drive cycles, calibration numbers and the signal catalogue.

The simulator shares *data*, not code, with the backend (ADR-007): it reads the generated
`contracts/signals.v1.json` and the notebook's `profiles/calibration.yaml`.
"""

import json
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

import numpy as np
import yaml

SIM_DIR = Path(__file__).resolve().parents[1]
REPO = SIM_DIR.parent
PROFILES_DIR = SIM_DIR / "profiles"
CONTRACTS_DIR = REPO / "contracts"

# Standard-normal quantile for p90 (p10 is the negative): used to turn p10/p50/p90
# summaries into distributions.
Z90 = 1.2816


@dataclass(frozen=True)
class VehicleProfile:
    id: str
    make: str
    model: str
    years: tuple[int, int]
    engine: str
    displacement_l: float
    cylinders: int
    power_kw: float
    torque_nm: float
    mass_kg: float
    cd_a_m2: float
    tyre_radius_m: float
    transmission: str
    gear_ratios: tuple[float, ...]
    final_drive: float
    upshift_kmh: tuple[float, ...]
    lockup_kmh: float
    idle_rpm_warm: float
    idle_rpm_cold: float
    redline_rpm: float
    thermostat_open_c: float
    operating_c: float
    idle_calc_load_pct: float
    fuel_tank_l: float
    supported: tuple[str, ...] = field(default_factory=tuple)

    def rpm_per_kmh(self, gear: int) -> float:
        """Engine RPM per km/h in a gear, with no converter slip."""
        wheel_rpm_per_kmh = (1000 / 60) / (2 * np.pi * self.tyre_radius_m)
        return wheel_rpm_per_kmh * self.gear_ratios[gear] * self.final_drive


@lru_cache
def load_profile(profile_id: str) -> VehicleProfile:
    data = yaml.safe_load((PROFILES_DIR / f"{profile_id}.yaml").read_text(encoding="utf-8"))
    for k in ("years", "gear_ratios", "upshift_kmh", "supported"):
        data[k] = tuple(data[k])
    p = VehicleProfile(**data)
    if len(p.upshift_kmh) != len(p.gear_ratios) - 1:
        raise ValueError(f"{profile_id}: need one upshift speed per gear change")
    unknown = set(p.supported) - set(catalogue())
    if unknown:
        raise ValueError(f"{profile_id}: supported lists unknown signals {sorted(unknown)}")
    return p


def profile_ids() -> list[str]:
    return sorted(
        p.stem for p in PROFILES_DIR.glob("*.yaml") if p.stem not in ("cycles", "calibration")
    )


@lru_cache
def cycles() -> dict:
    return yaml.safe_load((PROFILES_DIR / "cycles.yaml").read_text(encoding="utf-8"))


@lru_cache
def calibration() -> dict:
    return yaml.safe_load((PROFILES_DIR / "calibration.yaml").read_text(encoding="utf-8"))


@lru_cache
def catalogue() -> dict[str, dict]:
    doc = json.loads((CONTRACTS_DIR / "signals.v1.json").read_text(encoding="utf-8"))
    return {s["name"]: s for s in doc["signals"]}


def lognormal_from_pcts(p: dict) -> tuple[float, float]:
    """(mu, sigma) of a lognormal whose median and 90th percentile match a p10/p50/p90 summary."""
    mu = float(np.log(p["p50"]))
    sigma = float((np.log(p["p90"]) - np.log(p["p10"])) / (2 * Z90))
    return mu, sigma


def normal_from_pcts(p: dict) -> tuple[float, float]:
    """(mean, sd) of a normal matched to a p10/p50/p90 summary."""
    return float(p["p50"]), float((p["p90"] - p["p10"]) / (2 * Z90))

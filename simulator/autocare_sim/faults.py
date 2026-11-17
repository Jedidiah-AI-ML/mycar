"""Fault library: gradual faults that change `physics.Health` over days or kilometres.

    from autocare_sim.faults import fault
    simulate(car, days=30, seed=1, faults=[fault("vacuum_leak", onset=5)])

Definitions (SEED P3) live in `simulator/faults/faults.yaml`. A fault never touches the
physics code: it only moves Health parameters, and the physics turns that into symptoms.
"""

from dataclasses import dataclass, field, fields, replace
from functools import lru_cache

import yaml

from autocare_sim.config import SIM_DIR
from autocare_sim.physics import Health

FAULTS_DIR = SIM_DIR / "faults"
_HEALTH_FIELDS = {f.name for f in fields(Health)}


@lru_cache
def library() -> dict[str, dict]:
    lib = yaml.safe_load((FAULTS_DIR / "faults.yaml").read_text(encoding="utf-8"))
    for name, spec in lib.items():
        unknown = set(spec["health"]) - _HEALTH_FIELDS
        if unknown:
            raise ValueError(f"fault {name}: unknown Health fields {sorted(unknown)}")
        if spec["clock"] not in ("days", "km"):
            raise ValueError(f"fault {name}: clock must be days or km")
    return lib


@dataclass(frozen=True)
class Fault:
    name: str
    system: str
    clock: str  # "days" or "km"
    onset: float
    ramp: float
    shape: float
    end: dict[str, float] = field(default_factory=dict)  # Health field -> fully-failed value
    cylinder: int = 0  # misfire only

    def severity(self, days: float, km: float) -> float:
        """0 before onset, rising to 1 at onset + ramp."""
        x = days if self.clock == "days" else km
        if x <= self.onset:
            return 0.0
        return min(((x - self.onset) / self.ramp) ** self.shape, 1.0)

    def apply(self, h: Health, s: float) -> Health:
        if s <= 0:
            return h
        ok = Health()
        changes = {k: getattr(ok, k) + (v - getattr(ok, k)) * s for k, v in self.end.items()}
        if self.cylinder:
            changes["misfire_cylinder"] = self.cylinder
        return replace(h, **changes)


def fault(name: str, **overrides) -> Fault:
    """A fault from the library, with optional overrides (onset, ramp, shape, cylinder)."""
    spec = dict(library()[name])
    spec.update(overrides)
    return Fault(
        name=name,
        system=spec["system"],
        clock=spec["clock"],
        onset=float(spec["onset"]),
        ramp=float(spec["ramp"]),
        shape=float(spec["shape"]),
        end={k: float(v) for k, v in spec["health"].items()},
        cylinder=int(spec.get("cylinder", 0)),
    )


def health_from(faults: list[Fault]):
    """A `health_fn(days, km)` that applies every fault at its current severity."""

    def health_fn(days: float, km: float) -> Health:
        h = Health()
        for f in faults:
            h = f.apply(h, f.severity(days, km))
        return h

    return health_fn

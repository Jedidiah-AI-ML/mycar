"""The canonical signal catalogue (`app/config/signals.yaml`), loaded once."""

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import yaml

SIGNALS_FILE = Path(__file__).resolve().parents[1] / "config" / "signals.yaml"
GROUPS = {"fast", "slow", "start", "motion", "device"}


@dataclass(frozen=True)
class Signal:
    id: int
    name: str
    pid: str | None
    unit: str
    group: str
    dp: int
    min: float
    max: float
    desc: str

    def in_range(self, value: float) -> bool:
        return self.min <= value <= self.max


@dataclass(frozen=True)
class Catalogue:
    version: int
    signals: tuple[Signal, ...]

    @property
    def by_name(self) -> dict[str, Signal]:
        return {s.name: s for s in self.signals}

    @property
    def by_id(self) -> dict[int, Signal]:
        return {s.id: s for s in self.signals}


@lru_cache
def catalogue() -> Catalogue:
    data = yaml.safe_load(SIGNALS_FILE.read_text(encoding="utf-8"))
    sigs = tuple(Signal(**{**s, "pid": s.get("pid")}) for s in data["signals"])
    ids = [s.id for s in sigs]
    names = [s.name for s in sigs]
    if len(set(ids)) != len(ids) or len(set(names)) != len(names):
        raise RuntimeError("signals.yaml: duplicate id or name")
    bad = [s.name for s in sigs if s.group not in GROUPS or s.min > s.max]
    if bad:
        raise RuntimeError(f"signals.yaml: bad group or range for {bad}")
    return Catalogue(version=data["version"], signals=sigs)

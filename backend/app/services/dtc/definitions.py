"""Fault-code definitions: `app/config/dtc/definitions.yaml` (SEED D1, D2) and the
top-50 list (D3). Validated at load; a bad file fails loudly, never mid-explanation.

- `definitions()` / `get(code)`: our own description, system, severity, stop-safely flag,
  causes and escalation note. `get()` returns None for codes we don't have (they get a
  safe "not in our list yet" answer, never an LLM guess).
- `top50()`: the codes whose generic explanations are pre-generated and reviewed.
- `sync_to_db()`: copy into `dtc_definitions` (idempotent upsert).
"""

from functools import lru_cache
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.models.dtc import SEVERITIES

CONFIG_DIR = Path(__file__).resolve().parents[2] / "config" / "dtc"
SYSTEMS = (
    "fuel_air", "engine_sensors", "ignition", "cam_crank", "emissions", "evap", "cooling", "oil",
    "charging", "idle_speed", "throttle", "transmission", "control_module", "network",
    "brakes", "body",
)  # fmt: skip


class Definition(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    code: str = Field(pattern=r"^[PCBU][0-9A-F]{4}$")
    system: str
    severity: str
    stop_safely: bool = False
    desc: str = Field(min_length=10, max_length=300)
    causes: tuple[str, ...] = Field(min_length=1)
    escalation: str | None = Field(default=None, max_length=200)

    @field_validator("system")
    @classmethod
    def _system(cls, v: str) -> str:
        if v not in SYSTEMS:
            raise ValueError(f"unknown system {v!r}")
        return v

    @field_validator("severity")
    @classmethod
    def _severity(cls, v: str) -> str:
        if v not in SEVERITIES:
            raise ValueError(f"severity must be one of {SEVERITIES}")
        return v

    @model_validator(mode="after")
    def _stop_safely_matches(self) -> "Definition":
        if self.stop_safely != (self.severity == "stop_safely"):
            raise ValueError(f"{self.code}: stop_safely must match severity 'stop_safely'")
        return self


class Definitions(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    version: int
    codes: tuple[Definition, ...]

    @model_validator(mode="after")
    def _unique(self) -> "Definitions":
        seen = [d.code for d in self.codes]
        dupes = {c for c in seen if seen.count(c) > 1}
        if dupes:
            raise ValueError(f"duplicate codes {sorted(dupes)}")
        return self

    @property
    def by_code(self) -> dict[str, Definition]:
        return {d.code: d for d in self.codes}


def load(path: Path = CONFIG_DIR / "definitions.yaml") -> Definitions:
    return Definitions(**yaml.safe_load(path.read_text(encoding="utf-8")))


@lru_cache
def definitions() -> Definitions:
    return load()


def get(code: str) -> Definition | None:
    return definitions().by_code.get(code.strip().upper())


@lru_cache
def top50() -> tuple[str, ...]:
    data = yaml.safe_load((CONFIG_DIR / "top50.yaml").read_text(encoding="utf-8"))
    codes = tuple(data["codes"])
    missing = [c for c in codes if get(c) is None]
    if missing or len(set(codes)) != len(codes):
        raise ValueError(f"top50.yaml: undefined {missing} or duplicates")
    return codes


async def sync_to_db(session, defs: Definitions | None = None) -> int:
    """Upsert every definition into `dtc_definitions`. Returns how many. Caller commits."""
    from sqlalchemy import func
    from sqlalchemy.dialects.postgresql import insert

    from app.models import DtcDefinition

    defs = defs or definitions()
    rows = [
        {"code": d.code, "description": d.desc, "system": d.system, "severity": d.severity,
         "stop_safely": d.stop_safely, "causes": list(d.causes), "escalation": d.escalation,
         "version": defs.version}
        for d in defs.codes
    ]  # fmt: skip
    stmt = insert(DtcDefinition).values(rows)
    cols = ("description", "system", "severity", "stop_safely", "causes", "escalation", "version")
    await session.execute(
        stmt.on_conflict_do_update(
            index_elements=[DtcDefinition.code],
            set_={c: stmt.excluded[c] for c in cols} | {"updated_at": func.now()},
        )
    )
    return len(rows)

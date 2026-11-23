"""Everything the LLM may use to explain one fault code: its whole world (ADR-013).

Grounded generation: we look the facts up and hand them over; the model is told to use
nothing else. The context is a strict model:
- There is **no field** for a VIN, owner, email, position or device id, and unknown
  fields are rejected, so personal data can't leak into a prompt (NDPA, decision Q3).
- It serialises to canonical JSON, so the same facts always give the same SHA-256
  `input_hash`: the cache key (the same inputs never cost a second call).

`build_context()` (step 3) fills it from the database and the curated notes.
"""

import hashlib
import json
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

CODE_RE = re.compile(r"\b[PCBU][0-9A-F]{4}\b")


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class DefinitionFacts(_Strict):
    description: str
    system: str
    severity: Literal["info", "watch", "service_soon", "stop_safely"]
    stop_safely: bool
    causes: tuple[str, ...]
    escalation: str | None = None


class VehicleFacts(_Strict):
    make: str
    model: str
    year: int
    engine: str | None = None


class OtherCode(_Strict):
    code: str
    status: Literal["stored", "pending", "permanent"]
    description: str | None = None


class TripFacts(_Strict):
    """A recent trip, in figures only (no places, no times of day)."""

    distance_km: float | None
    idle_share: float | None
    max_speed_kmh: float | None
    harsh_brake_n: int = 0


class Note(_Strict):
    title: str
    text: str


class DtcContext(_Strict):
    code: str = Field(pattern=r"^[PCBU][0-9A-F]{4}$")
    status: Literal["stored", "pending", "permanent"]
    definition: DefinitionFacts | None  # None: not in our list (template answer, no LLM)
    vehicle: VehicleFacts | None = None  # None for the generic (pre-generated) explanations
    freeze_frame: dict[str, float] | None = None
    other_active_codes: tuple[OtherCode, ...] = ()
    recent_trips: tuple[TripFacts, ...] = ()
    indicators: tuple[dict, ...] = ()  # Phase 5 fills this slot
    notes: tuple[Note, ...] = ()

    @property
    def allowed_codes(self) -> set[str]:
        """The only codes an answer may mention."""
        return {self.code} | {o.code for o in self.other_active_codes}

    def canonical_json(self) -> str:
        return json.dumps(self.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))

    def input_hash(self, prompt_version: str) -> str:
        return hashlib.sha256(f"{prompt_version}\n{self.canonical_json()}".encode()).hexdigest()

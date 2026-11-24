"""Everything the LLM may use to explain one fault code: its whole world (ADR-013).

Grounded generation: we look the facts up and hand them over; the model is told to use
nothing else. The context is a strict model:
- There is **no field** for a VIN, owner, email, position or device id, and unknown
  fields are rejected, so personal data can't leak into a prompt (NDPA, decision Q3).
- It serialises to canonical JSON, so the same facts always give the same SHA-256
  `input_hash`: the cache key (the same inputs never cost a second call).

`build_context()` (app/services/dtc/build.py) fills it from the database and the curated
notes.
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


class UsageFacts(_Strict):
    """How the car has been used over the last 30 days, in coarse bands.

    Bands, not trip figures: the context is the cache key, so it must not change with every
    drive (a code active for weeks would otherwise be re-explained, and paid for, after each
    trip). Bands change only when the car's use really changes. No places, no times.
    """

    km_per_day: Literal["under 20", "20 to 50", "over 50"]
    idle_share: Literal["low (under 30 %)", "typical (30 to 50 %)", "high (over 50 %)"]
    braking: Literal["gentle", "moderate", "hard"]


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
    usage: UsageFacts | None = None  # None: no trips yet, or a generic explanation
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

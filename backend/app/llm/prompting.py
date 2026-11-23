"""Versioned prompt files (PRD 5: prompts are versioned files under app/llm/prompts/).

A prompt file has a `## system` and a `## user` section; `{placeholders}` in the user part
are filled per call. The version (e.g. "dtc_explain.v1") is part of every cache key, so
changing a prompt means a new file and fresh answers, never a silent edit of old ones.
"""

import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"


@dataclass(frozen=True)
class Prompt:
    version: str  # file stem, e.g. "dtc_explain.v1"
    system: str
    user: str

    def render_user(self, **values: str) -> str:
        # Plain replacement (not str.format): the prompt contains JSON braces.
        out = self.user
        for key, value in values.items():
            out = out.replace("{" + key + "}", value)
        if missing := re.findall(r"\{([a-z_]+)\}", out):
            raise KeyError(f"{self.version}: unfilled placeholders {missing}")
        return out


@lru_cache
def load_prompt(version: str) -> Prompt:
    text = (PROMPTS_DIR / f"{version}.md").read_text(encoding="utf-8")
    parts = re.split(r"^## (system|user)\s*$", text, flags=re.MULTILINE)
    sections = {parts[i]: parts[i + 1].strip() for i in range(1, len(parts) - 1, 2)}
    if set(sections) != {"system", "user"}:
        raise ValueError(f"{version}: needs exactly a '## system' and a '## user' section")
    return Prompt(version, sections["system"], sections["user"])

"""The pre-generated, human-reviewed explanations of the top-50 codes (Phase 4 step 4).

`scripts/pregenerate_dtc.py` asks the real model once per code and status (stored, pending)
using the **generic** context (no car) and writes `app/config/dtc/explanations.v1.yaml`.
Every entry starts as `reviewed: false`; Wisdom corrects the text and sets `reviewed: true`.

At serve time (`served()`), an entry is used only if:
- it is reviewed;
- it still validates and passes the grounding check (so a hand edit can't break the rules);
- its `input_hash` still matches today's generic context. If a definition or note changes,
  the hash changes, and that code falls back to the live LLM / template until regenerated
  and reviewed again.

Regenerating keeps reviewed entries whose hash still matches: no re-buying, no re-reviewing.
"""

from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

import yaml
from pydantic import ValidationError

from app.core.config import get_settings
from app.llm.schemas import DtcExplanation, grounding_problems
from app.services.dtc import definitions as dd
from app.services.dtc.build import generic_context
from app.services.dtc.explain import PROMPT_VERSION, explain

FILE = dd.CONFIG_DIR / "explanations.v1.yaml"
STATUSES = ("stored", "pending")

HEADER = """\
# Pre-generated explanations of the top-50 codes (Phase 4 step 4). Written by
# scripts/pregenerate_dtc.py; reviewed by Wisdom (docs/review/dtc_explanations_v1.md).
#
# To approve one: fix the text if needed, then set `reviewed: true`. Only reviewed entries
# are served. Severity must stay equal to the definition's, and no other code may be
# mentioned (checked when loaded, and by a test).
"""


@dataclass
class Report:
    generated: list[tuple[str, str]] = field(default_factory=list)
    kept: list[tuple[str, str]] = field(default_factory=list)
    failed: dict[tuple[str, str], list[str]] = field(default_factory=dict)
    stopped: str | None = None  # no_key / over_budget / provider_error
    stop_reason: str | None = None


def read(path: Path = FILE) -> list[dict]:
    if not path.exists():
        return []
    return (yaml.safe_load(path.read_text(encoding="utf-8")) or {}).get("entries", [])


def write(entries: list[dict], path: Path = FILE) -> None:
    entries = sorted(entries, key=lambda e: (e["code"], STATUSES.index(e["status"])))
    body = yaml.safe_dump(
        {"version": 1, "prompt_version": PROMPT_VERSION, "entries": entries},
        sort_keys=False, allow_unicode=True, width=100,
    )  # fmt: skip
    path.write_text(HEADER + "\n" + body, encoding="utf-8")


def check(entry: dict) -> list[str]:
    """Why this entry can't be served ([] if it can), ignoring the review flag."""
    ctx = generic_context(entry["code"], entry["status"])
    if entry.get("input_hash") != ctx.input_hash(PROMPT_VERSION):
        return ["stale: the definition, notes or prompt changed since it was generated"]
    try:
        answer = DtcExplanation.model_validate(entry["explanation"])
    except ValidationError as e:
        return [f"schema: {err['loc']} {err['msg']}" for err in e.errors()]
    return grounding_problems(answer, ctx)


async def pregenerate(session, codes, *, force: bool = False, path: Path = FILE) -> Report:
    """Generate the missing (or stale) entries. Caller commits the logged LLM calls."""
    report = Report()
    existing = {(e["code"], e["status"]): e for e in read(path)}
    out = dict(existing)
    model = get_settings().llm_model
    for code in codes:
        for status in STATUSES:
            key = (code, status)
            old = existing.get(key)
            if old and not force and not check(old):
                report.kept.append(key)
                continue
            ctx = generic_context(code, status)
            res = await explain(session, ctx)
            if res.skipped in ("no_key", "over_budget", "provider_error"):
                report.stopped = res.skipped
                report.stop_reason = "; ".join(res.problems) or None
                write(list(out.values()), path)
                return report
            if res.source != "generic":
                report.failed[key] = res.problems or [res.skipped or "no answer"]
                continue  # never stored; an old entry, if any, stays (and stays unserved)
            out[key] = {
                "code": code, "status": status, "reviewed": False, "model": model,
                "prompt_version": res.prompt_version, "input_hash": res.input_hash,
                "explanation": res.explanation.model_dump(),
            }  # fmt: skip
            report.generated.append(key)
    write(list(out.values()), path)
    return report


@lru_cache
def _served(path: Path) -> dict[tuple[str, str], DtcExplanation]:
    return {
        (e["code"], e["status"]): DtcExplanation.model_validate(e["explanation"])
        for e in read(path)
        if e.get("reviewed") is True and not check(e)
    }


def served(code: str, status: str, path: Path = FILE) -> DtcExplanation | None:
    """The reviewed generic explanation for this code and status, if there is one."""
    return _served(path).get((code, status))

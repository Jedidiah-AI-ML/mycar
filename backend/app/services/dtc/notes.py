"""Pick the curated notes (app/knowledge/*.md, SEED D4) relevant to one fault code.

Keyword retrieval, kept simple and explainable, which is enough for a few dozen notes
(decision Q5: no vector database). Each note scores:
- 10 if it was written for this exact code,
-  3 if it covers the code's system (fuel_air, ignition, ...),
-  1 for each of its keywords found in the code's description and causes.
The best note is used if it scores at all; a 2nd or 3rd only if it scores at least 3 (a
system match or several shared words), so one stray word ("oil") doesn't pull in an
unrelated note. Ties go to the file name, so the same code always gets the same notes
(and so the same cache key).
"""

import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import yaml

from app.services.dtc.definitions import Definition

KNOWLEDGE_DIR = Path(__file__).resolve().parents[2] / "knowledge"
WORD = re.compile(r"[a-z]+")


@dataclass(frozen=True)
class KnowledgeNote:
    name: str
    title: str
    codes: frozenset[str]
    systems: frozenset[str]
    keywords: frozenset[str]
    text: str


def parse(path: Path) -> KnowledgeNote:
    raw = path.read_text(encoding="utf-8")
    m = re.match(r"^---\n(.*?)\n---\n(.*)$", raw, flags=re.DOTALL)
    if not m:
        raise ValueError(f"{path.name}: needs a --- header ---")
    head = yaml.safe_load(m.group(1))
    missing = {"title", "codes", "systems", "keywords"} - set(head)
    if missing:
        raise ValueError(f"{path.name}: header lacks {sorted(missing)}")
    return KnowledgeNote(
        name=path.stem,
        title=head["title"],
        codes=frozenset(head["codes"]),
        systems=frozenset(head["systems"]),
        keywords=frozenset(k.lower() for k in head["keywords"]),
        text=" ".join(m.group(2).split()),
    )


@lru_cache
def notes() -> tuple[KnowledgeNote, ...]:
    return tuple(parse(p) for p in sorted(KNOWLEDGE_DIR.glob("*.md")) if p.name != "README.md")


def score(note: KnowledgeNote, code: str, d: Definition | None) -> int:
    s = 10 if code in note.codes else 0
    if d is not None:
        s += 3 if d.system in note.systems else 0
        words = set(WORD.findall(" ".join([d.desc, *d.causes]).lower()))
        s += len(note.keywords & words)
    return s


MIN_EXTRA = 3  # a 2nd/3rd note needs at least a system match (or 3 shared keywords)


def relevant(code: str, d: Definition | None, k: int = 3) -> list[KnowledgeNote]:
    ranked = sorted(((score(n, code, d), n.name, n) for n in notes()), key=lambda t: (-t[0], t[1]))
    picked = [n for i, (s, _, n) in enumerate(ranked[:k]) if s > 0 and (i == 0 or s >= MIN_EXTRA)]
    return picked

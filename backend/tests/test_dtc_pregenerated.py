"""Phase 4 step 4: pre-generated top-50 explanations (fake LLM; no network, no cost).

- each code gets a stored and a pending entry, unreviewed, which are not served
- an answer that fails validation or grounding is reported and never stored
- valid entries are kept on a re-run (no new calls); --force regenerates
- only reviewed, still-valid, up-to-date entries are served; a hand edit that breaks the
  rules, or a changed definition (stale hash), stops it being served
- the committed file is 100 % valid and grounded, covering all 50 codes x 2 (the PRD gate)
"""

import json
import re
from types import SimpleNamespace

import pytest
from pydantic import SecretStr

from app.core.config import get_settings
from app.llm import client
from app.services.dtc import definitions as dd
from app.services.dtc import pregenerated as pg
from app.services.dtc.review_sheet import render

CODES = ("P0171", "P0302", "P0420")


def answer_for(user_msg: str, *, severity: str | None = None, extra: str = "") -> dict:
    ctx = json.loads(user_msg.split("CONTEXT (JSON):\n", 1)[1].split("\n\nExplain", 1)[0])
    return {
        "summary": f"The engine computer noticed a problem with {ctx['code']}. {extra}".strip(),
        "severity": severity or ctx["definition"]["severity"],
        "likely_causes": [{"cause": ctx["definition"]["causes"][0], "why": "most common"}],
        "what_to_do_now": "Book a check with your mechanic soon.",
        "questions_for_mechanic": ["What did you test first?"],
        "confidence": "medium",
    }


class SmartFake:
    """Answers well, except for the (code, status) pairs told to misbehave."""

    def __init__(self, bad: dict | None = None):
        self.bad = bad or {}
        self.calls = 0

    async def __call__(self, **kw):
        self.calls += 1
        user = kw["messages"][1]["content"]
        code, status = re.search(r"fault code (\w+) \(status: (\w+)\)", user).groups()
        body = answer_for(user, **self.bad.get((code, status), {}))
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(body)))],
            usage=SimpleNamespace(prompt_tokens=1500, completion_tokens=400),
        )


@pytest.fixture
def fake(monkeypatch):
    monkeypatch.setattr(get_settings(), "llm_api_key", SecretStr("test-key"))
    monkeypatch.setattr(client, "_cost", lambda _r: 0.0005)

    def install(bad=None) -> SmartFake:
        f = SmartFake(bad)
        monkeypatch.setattr(client, "acompletion", f)
        return f

    return install


def review_all(path):
    entries = pg.read(path)
    for e in entries:
        e["reviewed"] = True
    pg.write(entries, path)
    pg._served.cache_clear()


async def test_generates_stored_and_pending_unreviewed(session, fake, tmp_path):
    f, path = fake(), tmp_path / "x.yaml"
    report = await pg.pregenerate(session, CODES, path=path)
    assert len(report.generated) == 6 and not report.failed and f.calls == 6
    entries = pg.read(path)
    assert {(e["code"], e["status"]) for e in entries} == {
        (c, s) for c in CODES for s in pg.STATUSES
    }
    assert all(e["reviewed"] is False and not pg.check(e) for e in entries)
    assert pg.served("P0171", "stored", path) is None  # not reviewed -> not served


async def test_failed_answers_are_reported_never_stored(session, fake, tmp_path):
    path = tmp_path / "x.yaml"
    fake({("P0302", "pending"): {"severity": "info"}, ("P0420", "stored"): {"extra": "See P0171."}})
    report = await pg.pregenerate(session, CODES, path=path)
    assert set(report.failed) == {("P0302", "pending"), ("P0420", "stored")}
    assert "severity" in report.failed[("P0302", "pending")][0]
    assert "P0171" in " ".join(report.failed[("P0420", "stored")])
    stored = {(e["code"], e["status"]) for e in pg.read(path)}
    assert ("P0302", "pending") not in stored and ("P0420", "stored") not in stored
    assert len(stored) == 4


async def test_rerun_keeps_valid_entries_and_force_regenerates(session, fake, tmp_path):
    path, f = tmp_path / "x.yaml", fake()
    await pg.pregenerate(session, CODES, path=path)
    review_all(path)
    f.calls = 0
    again = await pg.pregenerate(session, CODES, path=path)
    assert f.calls == 0 and len(again.kept) == 6
    assert all(e["reviewed"] for e in pg.read(path))  # the review survives
    forced = await pg.pregenerate(session, CODES, force=True, path=path)
    assert f.calls == 6 and len(forced.generated) == 6
    assert not any(e["reviewed"] for e in pg.read(path))  # new text needs a new review


async def test_only_reviewed_valid_current_entries_are_served(session, fake, tmp_path):
    path = tmp_path / "x.yaml"
    fake()
    await pg.pregenerate(session, CODES, path=path)
    review_all(path)
    assert pg.served("P0171", "stored", path).severity == dd.get("P0171").severity

    entries = pg.read(path)
    for e in entries:
        if (e["code"], e["status"]) == ("P0171", "stored"):
            e["explanation"]["summary"] += " Also check P0300."  # breaks grounding
        if (e["code"], e["status"]) == ("P0302", "stored"):
            e["explanation"]["severity"] = "info"  # breaks the severity rule
        if (e["code"], e["status"]) == ("P0420", "stored"):
            e["input_hash"] = "0" * 64  # as if the definition changed since
    pg.write(entries, path)
    pg._served.cache_clear()
    assert pg.served("P0171", "stored", path) is None
    assert pg.served("P0302", "stored", path) is None
    assert pg.served("P0420", "stored", path) is None
    assert pg.served("P0420", "pending", path) is not None
    assert "stale" in pg.check(next(e for e in entries if e["input_hash"] == "0" * 64))[0]


async def test_no_key_stops_without_calls(session, tmp_path):
    report = await pg.pregenerate(session, CODES, path=tmp_path / "x.yaml")
    assert report.stopped == "no_key" and not report.generated


async def test_review_sheet_lists_every_top50_code(session, fake, tmp_path):
    path = tmp_path / "x.yaml"
    fake()
    await pg.pregenerate(session, CODES, path=path)
    text = render(path)
    assert all(f"## {c}:" in text for c in dd.top50())
    assert "0 of 6 reviewed" in text and text.count("**not reviewed**") == 6


def test_the_committed_file_is_complete_valid_and_grounded():
    """The PRD gate: 100 % of the generated explanations are schema-valid and grounded."""
    if not pg.FILE.exists():
        pytest.skip("not generated yet (scripts/pregenerate_dtc.py)")
    entries = pg.read()
    keys = {(e["code"], e["status"]) for e in entries}
    assert keys == {(c, s) for c in dd.top50() for s in pg.STATUSES}
    bad = {(e["code"], e["status"]): p for e in entries if (p := pg.check(e))}
    assert not bad, bad


async def test_a_fatal_provider_error_stops_the_run_after_one_call(session, fake, tmp_path):
    class AuthenticationError(Exception):
        pass

    n = 0

    async def bad_key(**kw):
        nonlocal n
        n += 1
        raise AuthenticationError("Incorrect API key provided")

    fake()
    client.acompletion = bad_key  # the fixture's monkeypatch restores it
    report = await pg.pregenerate(session, CODES, path=tmp_path / "x.yaml")
    assert report.stopped == "provider_error" and n == 1 and not report.generated


def test_every_reviewed_entry_in_the_committed_file_is_served():
    if not pg.FILE.exists():
        pytest.skip("not generated yet")
    pg._served.cache_clear()
    reviewed = [(e["code"], e["status"]) for e in pg.read() if e.get("reviewed") is True]
    assert all(pg.served(c, s) is not None for c, s in reviewed), "a reviewed entry isn't served"

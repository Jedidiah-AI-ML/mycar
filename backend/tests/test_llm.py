"""Phase 4 step 2 gate: the LLM layer, with a fake LLM (no network, no cost).

- a valid answer is used; bad JSON -> retried, then the template
- an invented code -> rejected; a changed severity -> rejected
- over budget -> template with no call; no key -> template with no call
- every attempt is logged with tokens and cost
- the template is always valid and grounded, for every definition
"""

import json
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace

import pytest
from pydantic import SecretStr, ValidationError
from sqlalchemy import select

from app.core.config import get_settings
from app.llm import client
from app.llm.prompting import Prompt, load_prompt
from app.llm.schemas import DtcExplanation, grounding_problems
from app.models import LlmCall
from app.services.dtc import definitions as dd
from app.services.dtc.context import DtcContext, OtherCode, VehicleFacts
from app.services.dtc.explain import PROMPT_VERSION, explain
from app.services.dtc.templates import PENDING, template_explanation

COROLLA = VehicleFacts(make="Toyota", model="Corolla", year=2010, engine="2ZR-FE 1.8L I4")


def ctx(code="P0171", status="stored", vehicle=COROLLA, others=()) -> DtcContext:
    d = dd.get(code)
    definition = None
    if d is not None:
        definition = {
            "description": d.desc, "system": d.system, "severity": d.severity,
            "stop_safely": d.stop_safely, "causes": d.causes, "escalation": d.escalation,
        }  # fmt: skip
    return DtcContext(code=code, status=status, definition=definition, vehicle=vehicle,
                      freeze_frame={"rpm": 780, "coolant": 91, "stft_b1": 18.0},
                      other_active_codes=tuple(others))  # fmt: skip


def good_answer(severity="service_soon", **over) -> dict:
    a = {
        "summary": "Your engine is getting a bit too much air for the fuel it gets, so it runs "
                   "lean. The computer is adding fuel to make up for it.",
        "severity": severity,
        "likely_causes": [{"cause": "Vacuum leak", "why": "Fuel trim is high at idle."},
                          {"cause": "Dirty air flow sensor", "why": "It under-reads air."}],
        "what_to_do_now": "Have it checked in the next few days.",
        "questions_for_mechanic": ["Did you smoke-test the intake for leaks?"],
        "confidence": "medium",
    }  # fmt: skip
    return a | over


class FakeLLM:
    """Replays a script of answers: a dict (valid JSON), a str (raw text) or an exception."""

    def __init__(self, *script):
        self.script = list(script)
        self.calls: list[dict] = []

    async def __call__(self, **kw):
        self.calls.append(kw)
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        text = json.dumps(item) if isinstance(item, dict) else item
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=text))],
            usage=SimpleNamespace(prompt_tokens=1200, completion_tokens=350),
        )


@pytest.fixture
def llm(monkeypatch):
    """LLM switched on with a fake behind it; each call costs $0.0004."""
    monkeypatch.setattr(get_settings(), "llm_api_key", SecretStr("test-key"))
    monkeypatch.setattr(client, "_cost", lambda _r: 0.0004)

    def install(*script) -> FakeLLM:
        fake = FakeLLM(*script)
        monkeypatch.setattr(client, "acompletion", fake)
        return fake

    return install


async def calls(session) -> list[LlmCall]:
    return list((await session.scalars(select(LlmCall).order_by(LlmCall.id))).all())


# ---------------------------------------------------------------- the happy path


async def test_valid_answer_is_used_and_logged(session, llm):
    fake = llm(good_answer())
    r = await explain(session, ctx())
    assert r.source == "personalised" and r.attempts == 1 and not r.problems
    assert r.explanation.severity == "service_soon"
    (row,) = await calls(session)
    assert (row.ok, row.tokens_in, row.tokens_out) == (True, 1200, 350)
    assert row.cost_usd == Decimal("0.000400")
    assert row.prompt_version == PROMPT_VERSION and row.input_hash == r.input_hash
    assert row.model == "openai/gpt-4o-mini" and row.purpose == "dtc_explain"
    sent = fake.calls[0]
    assert sent["response_format"] == {"type": "json_object"} and sent["api_key"] == "test-key"
    assert "Use ONLY the facts" in sent["messages"][0]["content"]
    assert '"code":"P0171"' in sent["messages"][1]["content"]


async def test_without_a_car_it_is_a_generic_explanation(session, llm):
    llm(good_answer())
    assert (await explain(session, ctx(vehicle=None))).source == "generic"


# ---------------------------------------------------------------- rejected answers


async def test_bad_json_is_retried_once_then_used(session, llm):
    llm("Sure! Here's your explanation: {", good_answer())
    r = await explain(session, ctx())
    assert r.source == "personalised" and r.attempts == 2
    assert [c.ok for c in await calls(session)] == [False, True]
    assert (await calls(session))[0].error.startswith("not JSON")


async def test_bad_json_twice_falls_back_to_the_template(session, llm):
    llm("not json", "{still not")
    r = await explain(session, ctx())
    assert r.source == "template" and r.attempts == 2 and len(r.problems) == 2
    assert r.explanation == template_explanation(ctx())
    assert len(await calls(session)) == 2


async def test_invented_code_is_rejected(session, llm):
    bad = good_answer(summary="This often comes with P0300 and P0420, so check those first.")
    llm(bad, bad)
    r = await explain(session, ctx())
    assert r.source == "template"
    assert any("not in its context" in p and "P0300" in p for p in r.problems)


async def test_codes_that_are_in_the_context_are_fine(session, llm):
    ok = good_answer(summary="With P0300 also active on this car, a misfire may be adding to it.")
    llm(ok)
    r = await explain(session, ctx(others=[OtherCode(code="P0300", status="stored")]))
    assert r.source == "personalised"


async def test_changed_severity_is_rejected(session, llm):
    llm(good_answer(severity="stop_safely"), good_answer(severity="info"))
    r = await explain(session, ctx())
    assert r.source == "template" and r.explanation.severity == "service_soon"
    assert all("differs from the definition" in p for p in r.problems)


async def test_wrong_shape_is_rejected(session, llm):
    missing = good_answer()
    del missing["questions_for_mechanic"]
    llm(missing, good_answer(confidence="certain"))
    r = await explain(session, ctx())
    assert r.source == "template" and all(p.startswith("schema") for p in r.problems)


async def test_provider_errors_are_logged_and_fall_back(session, llm):
    llm(TimeoutError("read timed out"), TimeoutError("read timed out"))
    r = await explain(session, ctx())
    assert r.source == "template"
    rows = await calls(session)
    assert len(rows) == 2 and all(not c.ok and "TimeoutError" in c.error for c in rows)


# ---------------------------------------------------------------- no call at all


async def test_no_key_means_no_call(session, monkeypatch):
    fake = FakeLLM(good_answer())
    monkeypatch.setattr(client, "acompletion", fake)
    r = await explain(session, ctx())  # the test harness sets LLM_API_KEY=""
    assert (r.source, r.skipped, fake.calls) == ("template", "no_key", [])
    assert await calls(session) == []


async def test_over_budget_means_no_call(session, llm):
    session.add(LlmCall(purpose="dtc_explain", model="m", prompt_version="v1",
                        input_hash="x" * 64, cost_usd=Decimal("2.000001"), ok=True,
                        ts=datetime.now(UTC)))  # fmt: skip
    await session.flush()
    fake = llm(good_answer())
    r = await explain(session, ctx())
    assert (r.source, r.skipped, fake.calls) == ("template", "over_budget", [])


async def test_last_months_spend_does_not_count(session, llm):
    session.add(LlmCall(purpose="dtc_explain", model="m", prompt_version="v1",
                        input_hash="x" * 64, cost_usd=Decimal("50"), ok=True,
                        ts=datetime(2000, 1, 15, tzinfo=UTC)))  # fmt: skip
    await session.flush()
    llm(good_answer())
    assert (await explain(session, ctx())).source == "personalised"


async def test_unknown_code_gets_a_template_and_no_call(session, llm):
    fake = llm(good_answer())
    r = await explain(session, ctx(code="P1234"))
    assert r.skipped == "no_definition" and fake.calls == []
    assert "isn't in AutoCare's list" in r.explanation.summary


# ---------------------------------------------------------------- templates


@pytest.mark.parametrize("status", ["stored", "pending"])
def test_template_is_valid_and_grounded_for_every_definition(status):
    for d in dd.definitions().codes:
        c = ctx(code=d.code, status=status)
        t = template_explanation(c)
        DtcExplanation.model_validate(t.model_dump())  # round-trips the schema
        assert grounding_problems(t, c) == [], d.code
        assert t.severity == d.severity
        assert (PENDING in t.summary) == (status == "pending")


def test_template_tells_you_to_stop_for_stop_safely_codes():
    t = template_explanation(ctx(code="P0217"))
    assert t.severity == "stop_safely" and t.what_to_do_now.startswith("Stop when it is safe")


# ---------------------------------------------------------------- prompt and context


def test_prompt_file_and_rendering():
    p = load_prompt(PROMPT_VERSION)
    assert p.version == "dtc_explain.v1" and "## " not in p.system
    out = p.render_user(context_json='{"a": 1}', code="P0171", status="stored")
    assert '{"a": 1}' in out and "P0171" in out
    with pytest.raises(KeyError):
        p.render_user(code="P0171")  # context_json and status left unfilled
    with pytest.raises(KeyError):
        Prompt("x", "s", "{oops}").render_user()


def test_context_cannot_carry_personal_data():
    fields = set(DtcContext.model_fields) | set(VehicleFacts.model_fields)
    for forbidden in ("vin", "owner", "email", "user_id", "lat", "lon", "position", "device_id"):
        assert forbidden not in fields
    with pytest.raises(ValidationError):
        DtcContext(code="P0171", status="stored", definition=None, vin="JTDBU4EE9A9123456")
    with pytest.raises(ValidationError):
        VehicleFacts(make="Toyota", model="Corolla", year=2010, vin="JTDBU4EE9A9123456")


def test_input_hash_is_stable_and_sensitive():
    a, b = ctx(), ctx()
    assert a.input_hash("v1") == b.input_hash("v1")
    assert a.input_hash("v1") != a.input_hash("v2")  # a new prompt version -> fresh answers
    assert a.input_hash("v1") != ctx(status="pending").input_hash("v1")


# ---------------------------------------------------------------- the real LiteLLM path


async def test_real_litellm_response_objects_fit_the_client(session, monkeypatch):
    """LiteLLM's own `mock_response` builds a genuine response object (usage, choices,
    cost lookup) without any network call: proves our parsing fits the real library."""
    from functools import partial

    monkeypatch.setattr(get_settings(), "llm_api_key", SecretStr("test-key"))
    real = client._litellm().acompletion
    monkeypatch.setattr(
        client, "acompletion", partial(real, mock_response=json.dumps(good_answer()))
    )
    r = await explain(session, ctx())
    assert r.source == "personalised" and r.explanation.confidence == "medium"
    (row,) = await calls(session)
    assert row.ok and row.tokens_in > 0 and row.tokens_out > 0
    assert row.cost_usd >= 0  # priced from LiteLLM's bundled table (no download)

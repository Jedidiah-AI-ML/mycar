"""Phase 4 step 5 gate: the dtc.interpret stage and the codes API (fake LLM, no cost).

- role matrix (viewer+ reads; strangers 404; admin usage is admin-only)
- pending codes use the pending message (PRD 5)
- a second identical run makes no LLM call (the cache); a rejected answer isn't re-bought
- severity always equals the definition's
- an unknown code still gets a template answer that says so
- no key / over budget -> reviewed generic or template, retried later; an existing
  personalised answer stays on show
"""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select

from app.core.config import get_settings
from app.jobs.handlers.dtc_interpret import interpret_trip_codes
from app.llm.schemas import DtcExplanation
from app.models import DtcEvent, DtcStatus, Insight, Trip, TripStatus
from app.services.dtc import definitions as dd
from app.services.dtc import interpret as itp
from app.services.dtc import pregenerated
from app.services.dtc.build import generic_context
from app.services.dtc.templates import PENDING, template_explanation
from tests.test_devices import make_admin
from tests.test_dtc_pregenerated import fake  # noqa: F401 - fixture
from tests.test_vehicles import add_vehicle, scene  # noqa: F401 - fixture

NOW = datetime.now(UTC)


@pytest.fixture(autouse=True)
def no_reviewed_generics(monkeypatch):
    """Tests here don't depend on which codes Wisdom has reviewed in explanations.v1.yaml:
    the fallback is the template unless a test installs a reviewed answer itself."""
    monkeypatch.setattr(pregenerated, "served", lambda code, status: None)


async def add_code(session, vid, code, status=DtcStatus.stored, *, cleared=False, frame=None):
    e = DtcEvent(vehicle_id=uuid.UUID(str(vid)), code=code, status=status,
                 first_seen=NOW - timedelta(days=2), last_seen=NOW,
                 cleared_at=NOW if cleared else None, freeze_frame=frame)  # fmt: skip
    session.add(e)
    await session.commit()
    return e


async def run(session, vid):
    out = await itp.interpret_vehicle(session, uuid.UUID(str(vid)), commit_each=True)
    await session.commit()
    return out


async def codes(client, sc, who="viewer"):
    r = await client.get(f"/vehicles/{sc.vid}/codes", headers=getattr(sc, who).headers)
    assert r.status_code == 200, r.text
    return r.json()


def first(body, status, code):
    return next(c for c in body[status] if c["code"] == code)


# ---------------------------------------------------------------- roles


EXPECTED = {
    #             owner manager viewer stranger
    "list":       (200, 200, 200, 404),
    "detail":     (200, 200, 200, 404),
    "llm_usage":  (404, 404, 404, 404),  # admin only; 404 so it isn't advertised
}  # fmt: skip


@pytest.mark.parametrize("action", list(EXPECTED))
@pytest.mark.parametrize(
    "role_idx,role", list(enumerate(["owner", "manager", "viewer", "stranger"]))
)
async def test_codes_role_matrix(client, session, scene, action, role_idx, role):  # noqa: F811
    await add_code(session, scene.vid, "P0171")
    h = getattr(scene, role).headers
    base = f"/vehicles/{scene.vid}/codes"
    path = {"list": base, "detail": f"{base}/P0171", "llm_usage": "/admin/llm/usage"}[action]
    r = await client.get(path, headers=h)
    assert r.status_code == EXPECTED[action][role_idx], f"{role} {action}: {r.text}"


async def test_needs_a_login(client, scene):  # noqa: F811
    assert (await client.get(f"/vehicles/{scene.vid}/codes")).status_code == 401


# ---------------------------------------------------------------- without the LLM


async def test_no_codes(client, scene):  # noqa: F811
    b = await codes(client, scene)
    assert b == {"stop_safely": False, "stored": [], "pending": [], "permanent": [],
                 "recently_cleared": []}  # fmt: skip


async def test_before_interpretation_codes_get_the_template(client, session, scene):  # noqa: F811
    await add_code(session, scene.vid, "P0171", frame={"rpm": 800.0})
    await add_code(session, scene.vid, "P0302", DtcStatus.pending)
    b = await codes(client, scene)
    stored, pending = first(b, "stored", "P0171"), first(b, "pending", "P0302")
    assert stored["freeze_frame"] == {"rpm": 800.0} and stored["known"] is True
    assert stored["explanation"]["source"] == "template"
    assert stored["explanation"]["generated_at"] is None
    assert pending["explanation"]["summary"].startswith(PENDING)  # PRD 5 pending message


async def test_no_key_stores_the_template_and_makes_no_call(client, session, scene):  # noqa: F811
    await add_code(session, scene.vid, "P0420")
    out = await run(session, scene.vid)
    assert out.template == ["P0420:stored"]
    row = await session.scalar(select(Insight))
    assert row.source == "template" and row.input_hash is None  # retried once a key exists
    out = await run(session, scene.vid)  # replaced, not duplicated
    assert await session.scalar(select(func.count()).select_from(Insight)) == 1
    b = await codes(client, scene)
    assert first(b, "stored", "P0420")["explanation"]["generated_at"] is not None


async def test_reviewed_generic_beats_the_template(client, session, scene, monkeypatch):  # noqa: F811
    ctx = generic_context("P0420", "stored")
    reviewed = template_explanation(ctx).model_copy(update={"summary": "Reviewed by Wisdom: ok."})
    monkeypatch.setattr(pregenerated, "served", lambda c, s: reviewed if c == "P0420" else None)
    await add_code(session, scene.vid, "P0420")
    await add_code(session, scene.vid, "P0171")
    b = await codes(client, scene)  # also before interpretation
    assert first(b, "stored", "P0420")["explanation"]["source"] == "generic"
    out = await run(session, scene.vid)
    assert out.generic == ["P0420:stored"] and out.template == ["P0171:stored"]


async def test_an_unknown_code_gets_a_template_that_says_so(client, session, scene):  # noqa: F811
    await add_code(session, scene.vid, "P1234")
    await run(session, scene.vid)
    c = first(await codes(client, scene), "stored", "P1234")
    assert c["known"] is False and c["description"] is None and c["severity"] == "watch"
    assert "isn't in AutoCare's list" in c["explanation"]["summary"]
    r = await client.get(f"/vehicles/{scene.vid}/codes/P1234", headers=scene.viewer.headers)
    assert r.status_code == 200 and r.json()["known"] is False


# ---------------------------------------------------------------- with the (fake) LLM


async def test_personalised_once_then_cached(client, session, scene, fake):  # noqa: F811
    f = fake()
    await add_code(session, scene.vid, "P0171", frame={"rpm": 780.0})
    await add_code(session, scene.vid, "P0302", DtcStatus.pending)
    out = await run(session, scene.vid)
    assert sorted(out.personalised) == ["P0171:stored", "P0302:pending"] and f.calls == 2
    out = await run(session, scene.vid)  # same codes, same car, same usage
    assert sorted(out.cached) == ["P0171:stored", "P0302:pending"] and f.calls == 2
    b = await codes(client, scene)
    for status, code in (("stored", "P0171"), ("pending", "P0302")):
        c = first(b, status, code)
        assert c["explanation"]["source"] == "personalised"
        assert c["severity"] == c["explanation"]["severity"] == dd.get(code).severity


async def test_a_change_in_context_is_explained_again(session, scene, fake):  # noqa: F811
    f = fake()
    await add_code(session, scene.vid, "P0171")
    await run(session, scene.vid)
    await add_code(session, scene.vid, "P0174")  # another active code: P0171's context changes
    out = await run(session, scene.vid)
    assert sorted(out.personalised) == ["P0171:stored", "P0174:stored"] and f.calls == 3


async def test_a_rejected_answer_is_not_bought_twice(client, session, scene, fake):  # noqa: F811
    f = fake({("P0171", "stored"): {"severity": "info"}})  # breaks the severity rule
    await add_code(session, scene.vid, "P0171")
    out = await run(session, scene.vid)
    assert out.template == ["P0171:stored"] and f.calls == 2  # two attempts, then template
    out = await run(session, scene.vid)
    assert out.cached == ["P0171:stored"] and f.calls == 2
    c = first(await codes(client, scene), "stored", "P0171")
    assert c["explanation"]["source"] == "template"
    assert c["explanation"]["severity"] == dd.get("P0171").severity


async def test_over_budget_keeps_the_personalised_answer(client, session, scene, fake, monkeypatch):  # noqa: F811
    f = fake()
    await add_code(session, scene.vid, "P0171")
    await run(session, scene.vid)
    monkeypatch.setattr(get_settings(), "llm_monthly_budget_usd", 0.0001)  # now spent
    await add_code(session, scene.vid, "P0174")  # new context for P0171; new code P0174
    out = await run(session, scene.vid)
    assert out.kept == ["P0171:stored"] and out.template == ["P0174:stored"] and f.calls == 1
    b = await codes(client, scene)
    assert first(b, "stored", "P0171")["explanation"]["source"] == "personalised"
    monkeypatch.setattr(get_settings(), "llm_monthly_budget_usd", 2.0)  # next month
    out = await run(session, scene.vid)
    assert sorted(out.personalised) == ["P0171:stored", "P0174:stored"]


async def test_stop_safely_is_flagged_and_listed_first(client, session, scene):  # noqa: F811
    await add_code(session, scene.vid, "P0420")
    await add_code(session, scene.vid, "P0217")
    b = await codes(client, scene)
    assert b["stop_safely"] is True
    assert [c["code"] for c in b["stored"]] == ["P0217", "P0420"]
    assert b["stored"][0]["explanation"]["what_to_do_now"].startswith("Stop")


# ---------------------------------------------------------------- detail, history, cleared


async def test_detail_history_and_recently_cleared(client, session, scene):  # noqa: F811
    await add_code(session, scene.vid, "P0171", cleared=True)
    await add_code(session, scene.vid, "P0171", DtcStatus.pending)
    b = await codes(client, scene)
    assert [c["code"] for c in b["recently_cleared"]] == ["P0171"]
    assert b["recently_cleared"][0]["explanation"] is None
    r = await client.get(f"/vehicles/{scene.vid}/codes/P0171", headers=scene.viewer.headers)
    d = r.json()
    assert d["severity"] == dd.get("P0171").severity and d["causes"]
    assert [a["status"] for a in d["active"]] == ["pending"] and len(d["history"]) == 2
    h = scene.viewer.headers
    assert (await client.get(f"/vehicles/{scene.vid}/codes/P0300", headers=h)).status_code == 404
    assert (await client.get(f"/vehicles/{scene.vid}/codes/p0171", headers=h)).status_code == 422


# ---------------------------------------------------------------- pipeline + admin


async def test_the_pipeline_stage_interprets_the_trip_s_car(session, scene, fake):  # noqa: F811
    f = fake()
    vid = uuid.UUID(scene.vid)
    t = Trip(vehicle_id=vid, device_boot=0, device_trip=1, status=TripStatus.complete,
             started_at=NOW - timedelta(hours=1), ended_at=NOW)  # fmt: skip
    session.add(t)
    await add_code(session, scene.vid, "P0171")
    await interpret_trip_codes({"trip_id": str(t.id)}, session)
    await session.commit()
    assert f.calls == 1
    assert await session.scalar(select(Insight.source)) == "personalised"


async def test_admin_llm_usage(client, session, scene, fake):  # noqa: F811
    fake()
    await add_code(session, scene.vid, "P0171")
    await run(session, scene.vid)
    admin = await make_admin(client, session)
    r = await client.get("/admin/llm/usage", headers=admin.headers)
    assert r.status_code == 200, r.text
    u = r.json()
    assert u["calls"] == 1 and u["ok"] == 1 and u["failed"] == 0 and u["enabled"] is True
    assert u["spent_usd"] == pytest.approx(0.0005) and u["cap_usd"] == 2.0
    assert u["remaining_usd"] == pytest.approx(2.0 - 0.0005)
    assert u["tokens_in"] == 1500 and u["insights_by_source"] == {"personalised": 1}
    assert u["by_purpose"] == [{"purpose": "dtc_explain", "calls": 1, "cost_usd": 0.0005}]


def test_a_stored_explanation_round_trips_into_the_api_shape():
    e = template_explanation(generic_context("P0171", "stored"))
    assert DtcExplanation.model_validate(e.model_dump()) == e

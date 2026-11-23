"""Phase 4 step 1: fault-code definitions, the top-50 list, migration 0005 tables."""

import copy
import uuid
from decimal import Decimal

import pytest
import yaml
from pydantic import ValidationError
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError

from app.models import DtcDefinition, Insight, LlmCall
from app.services.dtc import definitions as dd
from tests.test_models import make_user, make_vehicle

RAW = yaml.safe_load((dd.CONFIG_DIR / "definitions.yaml").read_text(encoding="utf-8"))
# Every code the simulator's fault library can set (simulator/faults/dtc_rules.yaml).
SIMULATOR_CODES = {"P0128", "P0217", "P0171", "P0174", "P0101", "P0133", "P0420", "P0562",
                   "P0301", "P0302", "P0303", "P0304", "P0305", "P0306"}  # fmt: skip


# ---------------------------------------------------------------- the shipped config


def test_definitions_load_and_cover_what_the_simulator_sets():
    defs = dd.definitions()
    assert len(defs.codes) >= 150
    assert SIMULATOR_CODES <= set(defs.by_code)


def test_stop_safely_codes_are_the_deterministic_few():
    stop = {d.code for d in dd.definitions().codes if d.stop_safely}
    # PRD 5: overheating, oil pressure and similar; never left to the LLM.
    assert {"P0217", "P0524"} <= stop
    assert all(dd.get(c).severity == "stop_safely" for c in stop)


def test_top50_is_fifty_defined_codes():
    top = dd.top50()
    assert len(top) == 50 and len(set(top)) == 50
    assert all(dd.get(c) is not None for c in top)
    assert {"P0171", "P0300", "P0420", "P0128"} <= set(top)


def test_lookup_is_forgiving_and_unknown_codes_return_none():
    assert dd.get(" p0171 ").code == "P0171"
    assert dd.get("P1234") is None  # manufacturer-specific: not in our generic list


def test_every_definition_reads_like_a_sentence():
    for d in dd.definitions().codes:
        assert d.desc[0].isupper() and d.desc.endswith("."), d.code
        assert all(isinstance(c, str) and c for c in d.causes), d.code


# ---------------------------------------------------------------- bad config fails at load


def _mutated(fn) -> dict:
    data = copy.deepcopy(RAW)
    fn(data)
    return data


BAD = {
    "bad code format": lambda d: d["codes"][0].update(code="P01X"),
    "unknown system": lambda d: d["codes"][0].update(system="vibes"),
    "unknown severity": lambda d: d["codes"][0].update(severity="panic"),
    "stop_safely flag without the tier": lambda d: d["codes"][0].update(stop_safely=True),
    "tier without the flag": lambda d: d["codes"][0].update(severity="stop_safely"),
    "no causes": lambda d: d["codes"][0].update(causes=[]),
    "a cause that isn't text": lambda d: d["codes"][0].update(causes=[{"a": "b"}]),
    "duplicate code": lambda d: d["codes"].append(dict(d["codes"][0])),
    "unknown field": lambda d: d["codes"][0].update(severity_guess="x"),
}


@pytest.mark.parametrize("name", sorted(BAD))
def test_bad_definitions_fail_at_load(name):
    with pytest.raises(ValidationError):
        dd.Definitions(**_mutated(BAD[name]))


# ---------------------------------------------------------------- the database copy


async def test_sync_is_idempotent_and_matches_config(session):
    n = await dd.sync_to_db(session)
    assert n == len(dd.definitions().codes)
    await session.execute(update(DtcDefinition).where(DtcDefinition.code == "P0171")
                          .values(severity="info"))  # fmt: skip
    assert await dd.sync_to_db(session) == n  # repairs, adds nothing
    assert await session.scalar(select(func.count()).select_from(DtcDefinition)) == n
    row = await session.get(DtcDefinition, "P0171", populate_existing=True)
    assert row.severity == "service_soon" and row.causes[0].startswith("vacuum")
    p0217 = await session.get(DtcDefinition, "P0217")
    assert p0217.stop_safely is True


async def test_definition_table_checks(session):
    session.add(DtcDefinition(code="P9999", description="x" * 20, system="fuel_air",
                              severity="stop_safely", stop_safely=False, version=1))  # fmt: skip
    with pytest.raises(IntegrityError):  # the flag must match the tier
        await session.flush()


async def test_insight_cache_key_is_unique(session):
    v = await make_vehicle(session, await make_user(session))
    kw = {"vehicle_id": v.id, "kind": "dtc_explanation", "subject": "P0171:stored",
          "content": {"summary": "x"}, "source": "personalised", "prompt_version": "v1",
          "input_hash": "a" * 64}  # fmt: skip
    session.add(Insight(**kw))
    await session.flush()
    session.add(Insight(**kw))
    with pytest.raises(IntegrityError):
        await session.flush()


async def test_template_insights_without_a_hash_are_unique_too(session):
    """NULLS NOT DISTINCT: two hash-less (template) rows for the same subject can't pile up."""
    v = await make_vehicle(session, await make_user(session))
    kw = {"vehicle_id": v.id, "kind": "dtc_explanation", "subject": "P0171:stored",
          "content": {"summary": "x"}, "source": "template"}  # fmt: skip
    session.add(Insight(**kw))
    await session.flush()
    session.add(Insight(**kw))
    with pytest.raises(IntegrityError):
        await session.flush()


async def test_insight_source_is_checked(session):
    v = await make_vehicle(session, await make_user(session))
    session.add(Insight(vehicle_id=v.id, kind="dtc_explanation", subject="P0171:stored",
                        content={}, source="vibes"))  # fmt: skip
    with pytest.raises(IntegrityError):
        await session.flush()


async def test_llm_call_log(session):
    c = LlmCall(purpose="dtc_explain", model="openai/gpt-4o-mini", prompt_version="v1",
                input_hash="b" * 64, tokens_in=1200, tokens_out=400,
                cost_usd=Decimal("0.000420"), ok=True, latency_ms=900)  # fmt: skip
    session.add(c)
    await session.flush()
    await session.refresh(c)
    assert c.ts is not None and c.vehicle_id is None
    session.add(LlmCall(purpose="x", model="m", prompt_version="v1", input_hash="c" * 64,
                        cost_usd=Decimal("-1"), ok=False))  # fmt: skip
    with pytest.raises(IntegrityError):
        await session.flush()


async def test_deleting_a_vehicle_keeps_its_llm_costs(session):
    """Cost history survives (vehicle_id set NULL); the vehicle's insights go with it."""
    v = await make_vehicle(session, await make_user(session))
    session.add(LlmCall(purpose="dtc_explain", model="m", prompt_version="v1",
                        input_hash="d" * 64, ok=True, vehicle_id=v.id))  # fmt: skip
    session.add(Insight(vehicle_id=v.id, kind="dtc_explanation", subject="P0420:stored",
                        content={}, source="template"))  # fmt: skip
    await session.flush()
    await session.delete(v)
    await session.flush()
    assert await session.scalar(select(func.count()).select_from(Insight)) == 0
    row = await session.scalar(select(LlmCall))
    assert row is not None and row.vehicle_id is None
    assert uuid.UUID(str(v.id))

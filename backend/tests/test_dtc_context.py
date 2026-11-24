"""Phase 4 step 3 gate: the context the LLM sees.

- the right curated notes are retrieved for sample codes (and none for an unknown code)
- no personal data can reach the context: no field for it exists, unknown fields are
  rejected, and a car with a VIN, an owner and GPS positions still gives a context with
  none of them in its JSON
- the same inputs give the same context and the same hash; usage is banded, so another
  similar trip doesn't change the hash
"""

import json
import re
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from app.models import DtcEvent, DtcStatus, Trip, TripStatus
from app.services.dtc import definitions as dd
from app.services.dtc.build import build_context, generic_context, usage_bands
from app.services.dtc.context import DtcContext, VehicleFacts
from app.services.dtc.notes import notes, relevant
from tests.test_models import make_user, make_vehicle

PV = "dtc_explain.v1"
VIN = "JTDBR32E720123456"
EMAIL = "owner.private@example.com"


# ---------------------------------------------------------------- notes


def test_every_note_parses_and_has_a_header():
    ns = notes()
    assert len(ns) >= 15
    for n in ns:
        assert n.title and n.text and (n.codes or n.systems), n.name
        assert all(re.fullmatch(r"[PCBU][0-9A-F]{4}", c) for c in n.codes), n.name
        assert n.systems <= set(dd.SYSTEMS), n.name


@pytest.mark.parametrize(
    ("code", "expected_first", "also"),
    [
        ("P0171", "lean-mixture", {"air-flow-sensors"}),
        ("P0302", "misfire", set()),
        ("P0420", "catalyst", {"oxygen-sensors"}),
        ("P0128", "thermostat", {"overheating"}),
        ("P0562", "charging", set()),
        ("P0457", "evap-fuel-cap", set()),
        ("P0217", "overheating", {"thermostat"}),
    ],
)
def test_the_right_notes_for_sample_codes(code, expected_first, also):
    picked = [n.name for n in relevant(code, dd.get(code))]
    assert picked[0] == expected_first, picked
    assert also <= set(picked), picked
    assert len(picked) <= 3


def test_an_unknown_code_gets_no_notes():
    assert relevant("P1234", dd.get("P1234")) == []


def test_note_choice_is_deterministic():
    assert [n.name for n in relevant("P0171", dd.get("P0171"))] == [
        n.name for n in relevant("P0171", dd.get("P0171"))
    ]


# ---------------------------------------------------------------- privacy


def test_the_context_has_no_field_for_personal_data():
    fields = set(DtcContext.model_fields) | set(VehicleFacts.model_fields)
    for banned in ("vin", "owner", "user", "email", "position", "lat", "lon", "device"):
        assert not any(banned in f for f in fields), banned


def test_unknown_fields_are_rejected():
    with pytest.raises(ValidationError):
        DtcContext(code="P0171", status="stored", definition=None, vin=VIN)
    with pytest.raises(ValidationError):
        VehicleFacts(make="Toyota", model="Corolla", year=2010, vin=VIN)


async def _car_with_everything(session):
    """A car with a VIN, an owner with an email, a summarised trip with GPS, and codes."""
    owner = await make_user(session, EMAIL)
    v = await make_vehicle(session, owner, vin=VIN, engine="1ZZ-FE 1.8L I4", nickname="Mama's car")
    now = datetime.now(UTC)
    for d in (2, 5, 9):
        session.add(
            Trip(
                vehicle_id=v.id, device_boot=0, device_trip=d, status=TripStatus.complete,
                started_at=now - timedelta(days=d), ended_at=now - timedelta(days=d, minutes=-40),
                distance_km=25.0, distance_source="speed", driving_s=1800, idle_s=600,
                harsh_brake_n=1, summarised_at=now,
            )
        )  # fmt: skip
    events = {}
    for code, status in (("P0171", DtcStatus.stored), ("P0174", DtcStatus.pending)):
        frame = {"rpm": 780.0, "coolant_c": 91.0} if code == "P0171" else None
        e = DtcEvent(vehicle_id=v.id, code=code, status=status, first_seen=now, last_seen=now,
                     freeze_frame=frame)  # fmt: skip
        session.add(e)
        events[code] = e
    # an old, cleared code must not appear as "active"
    session.add(DtcEvent(vehicle_id=v.id, code="P0420", status=DtcStatus.stored, first_seen=now,
                         last_seen=now, cleared_at=now))  # fmt: skip
    await session.flush()
    return v, events


def _keys(obj) -> set[str]:
    """Every key in a JSON document, at any depth (freeze-frame signal names excluded)."""
    if isinstance(obj, dict):
        return {k for k in obj} | {
            k for key, v in obj.items() if key != "freeze_frame" for k in _keys(v)
        }
    if isinstance(obj, list):
        return {k for v in obj for k in _keys(v)}
    return set()


async def test_a_full_car_gives_a_context_without_personal_data(session):
    v, events = await _car_with_everything(session)
    ctx = await build_context(session, v, events["P0171"])
    text = ctx.canonical_json()
    for secret in (VIN, EMAIL, "Mama", str(v.id)):
        assert secret not in text, secret
    keys = _keys(json.loads(text))
    for banned in ("vin", "owner", "user", "email", "lat", "lon", "position", "device", "id"):
        assert banned not in keys, banned
    assert ctx.vehicle == VehicleFacts(make="Toyota", model="Corolla", year=2010,
                                       engine="1ZZ-FE 1.8L I4")  # fmt: skip
    assert ctx.freeze_frame == {"coolant_c": 91.0, "rpm": 780.0}
    assert [o.code for o in ctx.other_active_codes] == ["P0174"]  # not the cleared P0420
    assert ctx.allowed_codes == {"P0171", "P0174"}
    assert ctx.definition is not None and ctx.definition.severity == dd.get("P0171").severity
    assert [n.title for n in ctx.notes][0].startswith("Lean mixture")
    # 75 km over 30 days, 25 % idle, 4 brakes per 100 km
    assert ctx.usage is not None
    assert (ctx.usage.km_per_day, ctx.usage.braking) == ("under 20", "gentle")
    assert ctx.usage.idle_share.startswith("low")


async def test_no_trips_means_no_usage(session):
    v = await make_vehicle(session, await make_user(session))
    now = datetime.now(UTC)
    e = DtcEvent(vehicle_id=v.id, code="P0302", status=DtcStatus.stored, first_seen=now,
                 last_seen=now)  # fmt: skip
    session.add(e)
    await session.flush()
    ctx = await build_context(session, v, e)
    assert ctx.usage is None and ctx.other_active_codes == () and ctx.freeze_frame is None


# ---------------------------------------------------------------- stable hash


async def test_same_inputs_same_context_same_hash(session):
    v, events = await _car_with_everything(session)
    a = await build_context(session, v, events["P0171"])
    b = await build_context(session, v, events["P0171"])
    assert a == b and a.input_hash(PV) == b.input_hash(PV)
    assert a.input_hash(PV) != a.input_hash("dtc_explain.v2")  # a new prompt re-explains


async def test_another_similar_trip_does_not_change_the_hash(session):
    v, events = await _car_with_everything(session)
    before = (await build_context(session, v, events["P0171"])).input_hash(PV)
    now = datetime.now(UTC)
    session.add(Trip(vehicle_id=v.id, device_boot=0, device_trip=99, status=TripStatus.complete,
                     started_at=now - timedelta(hours=3), ended_at=now - timedelta(hours=2),
                     distance_km=20.0, distance_source="speed", driving_s=1500, idle_s=500,
                     harsh_brake_n=0, summarised_at=now))  # fmt: skip
    await session.flush()
    after = (await build_context(session, v, events["P0171"])).input_hash(PV)
    assert before == after


def test_generic_context_is_stable_and_carless():
    a, b = generic_context("P0420", "stored"), generic_context("P0420", "stored")
    assert a.input_hash(PV) == b.input_hash(PV)
    assert a.vehicle is None and a.usage is None and a.freeze_frame is None
    assert generic_context("P1234", "stored").definition is None


@pytest.mark.parametrize(
    ("km", "idle_s", "driving_s", "brakes", "expected"),
    [
        (300, 100, 900, 0, ("under 20", "low (under 30 %)", "gentle")),
        (900, 400, 600, 60, ("20 to 50", "typical (30 to 50 %)", "moderate")),
        (2400, 700, 300, 400, ("over 50", "high (over 50 %)", "hard")),
        (0, 0, 0, 0, ("under 20", "low (under 30 %)", "gentle")),
    ],
)
def test_usage_bands(km, idle_s, driving_s, brakes, expected):
    u = usage_bands(km, 30, idle_s, driving_s, brakes)
    assert (u.km_per_day, u.idle_share, u.braking) == expected

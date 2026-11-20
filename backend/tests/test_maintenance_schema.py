"""Phase 3 step 3: database rules for maintenance (migration 0004) and the rules sync."""

import copy
import uuid
from datetime import UTC, date, datetime

import pytest
import yaml
from sqlalchemy import delete, func, insert, select, text
from sqlalchemy.exc import IntegrityError

from app.models import (
    OdometerReading,
    ServiceItem,
    ServiceRecord,
    ServiceRule,
    Trip,
    Vehicle,
    reading_minutes,
)
from app.services import service_rules as sr
from tests.test_models import make_user, make_vehicle

NOW = datetime(2026, 11, 20, 12, 0, tzinfo=UTC)


async def synced(session):
    await sr.sync_to_db(session)
    await session.flush()


async def car(session, **kw) -> Vehicle:
    return await make_vehicle(session, await make_user(session), **kw)


# ---------------------------------------------------------------- rules sync


async def _rules(session) -> list:
    cols = (ServiceRule.item_key, ServiceRule.variant, ServiceRule.interval_km,
            ServiceRule.interval_months)  # fmt: skip
    stmt = select(*cols).order_by(ServiceRule.item_key, ServiceRule.variant)
    return (await session.execute(stmt)).all()


async def test_sync_copies_config_and_is_idempotent(session):
    n_items, n_rules = await sr.sync_to_db(session)
    assert n_items == 10 and n_rules == len(sr.rules().rules)
    first = await _rules(session)
    for _ in range(2):
        await sr.sync_to_db(session)
    again = await _rules(session)
    assert again == first
    assert await session.scalar(select(func.count()).select_from(ServiceItem)) == 10
    oil = {v: (km, mo) for k, v, km, mo in first if k == "engine_oil"}
    assert oil == {"mineral": (5000, 3), "semi_synthetic": (5000, 3), "full_synthetic": (7500, 6)}


async def test_sync_replaces_rules_but_keeps_items_with_records(session):
    """A record points at its item; changing the config never deletes that item."""
    await synced(session)
    v = await car(session)
    session.add(ServiceRecord(vehicle_id=v.id, item_key="coolant", done_on=date(2026, 1, 5)))
    await session.flush()
    data = yaml.safe_load((sr.CONFIG_DIR / "service_rules.yaml").read_text(encoding="utf-8"))
    d = copy.deepcopy(data)
    d["overrides"] = [{"item": "coolant", "make": "Toyota", "km": 100000, "months": 60}]
    items = {k: sr.Item(**x) for k, x in d.pop("items").items()}
    await sr.sync_to_db(session, cfg=sr.RulesConfig(items=items, **d))
    n = await session.scalar(select(func.count()).select_from(ServiceRule)
                             .where(ServiceRule.make == "Toyota"))  # fmt: skip
    assert n == 1
    await sr.sync_to_db(session)  # back to the shipped config: the override is gone
    assert await session.scalar(select(func.count()).select_from(ServiceRule)
                                .where(ServiceRule.make.is_not(None))) == 0  # fmt: skip
    assert await session.get(ServiceItem, "coolant") is not None


async def test_one_rule_per_scope_even_with_nulls(session):
    """NULLS NOT DISTINCT: two *default* rules (all scope columns NULL) can't coexist."""
    await synced(session)
    session.add(ServiceRule(item_key="coolant", profile="ng_severe", interval_km=1,
                            interval_months=1))  # fmt: skip
    with pytest.raises(IntegrityError):
        await session.flush()


@pytest.mark.parametrize("kw", [{"interval_km": 0}, {"interval_months": 0}])
async def test_rule_intervals_positive(session, kw):
    await synced(session)
    fields = {"item_key": "coolant", "profile": "test", "interval_km": 10, "interval_months": 1}
    session.add(ServiceRule(**(fields | kw)))
    with pytest.raises(IntegrityError):
        await session.flush()


# ---------------------------------------------------------------- service records


async def test_service_record_rules(session):
    await synced(session)
    v = await car(session)
    ok = ServiceRecord(vehicle_id=v.id, item_key="engine_oil", done_on=date(2026, 10, 1),
                       odometer_km=123_456, before_install=True)  # fmt: skip
    session.add(ok)
    await session.flush()
    await session.refresh(ok)
    assert ok.created_at is not None and ok.before_install is True


@pytest.mark.parametrize(
    "kw",
    [
        {"item_key": "flux_capacitor"},  # not a known item (FK)
        {"done_on": date(1985, 1, 1)},  # implausibly old
        {"odometer_km": -1},
        {"odometer_km": 1_500_001},
    ],
)
async def test_bad_service_records_rejected(session, kw):
    await synced(session)
    v = await car(session)
    fields = {"vehicle_id": v.id, "item_key": "engine_oil", "done_on": date(2026, 1, 1)}
    session.add(ServiceRecord(**(fields | kw)))
    with pytest.raises(IntegrityError):
        await session.flush()


async def test_item_with_records_cannot_be_deleted(session):
    await synced(session)
    v = await car(session)
    session.add(ServiceRecord(vehicle_id=v.id, item_key="coolant", done_on=date(2026, 1, 5)))
    await session.flush()
    with pytest.raises(IntegrityError):
        await session.execute(delete(ServiceItem).where(ServiceItem.key == "coolant"))


# ---------------------------------------------------------------- odometer, vehicles, trips


@pytest.mark.parametrize("kw", [{"km": -1}, {"km": 1_500_001}, {"source": "guess"}])
async def test_bad_odometer_readings_rejected(session, kw):
    v = await car(session)
    session.add(OdometerReading(**({"vehicle_id": v.id, "km": 1000, "source": "user"} | kw)))
    with pytest.raises(IntegrityError):
        await session.flush()


async def test_odometer_reading_defaults(session):
    v = await car(session)
    r = OdometerReading(vehicle_id=v.id, km=120_000, source="install")
    session.add(r)
    await session.flush()
    await session.refresh(r)
    assert r.mismatch is False and r.ts is not None


async def test_vehicle_oil_type(session):
    v = await car(session, oil_type="full_synthetic")
    assert v.oil_type == "full_synthetic"
    with pytest.raises(IntegrityError):
        await car(session, oil_type="olive")


@pytest.mark.parametrize(
    "kw", [{"distance_km": -0.1}, {"idle_share": 1.5}, {"distance_source": "guess"}]
)
async def test_trip_summary_checks(session, kw):
    v = await car(session)
    session.add(Trip(vehicle_id=v.id, device_boot=0, device_trip=1, **kw))
    with pytest.raises(IntegrityError):
        await session.flush()


async def test_trip_summary_defaults(session):
    v = await car(session)
    t = Trip(vehicle_id=v.id, device_boot=0, device_trip=1, distance_km=12.5,
             distance_source="speed+gnss", idle_share=0.42)  # fmt: skip
    session.add(t)
    await session.flush()
    await session.refresh(t)
    assert (t.harsh_brake_n, t.harsh_accel_n) == (0, 0)


# ---------------------------------------------------------------- reading_minutes


async def test_reading_minutes_one_row_per_trip_signal_minute(session):
    v = await car(session)
    row = {"vehicle_id": v.id, "trip_id": uuid.uuid4(), "signal_id": 1, "minute": NOW,
           "n": 60, "mean": 812.5, "min": 790.0, "max": 840.0}  # fmt: skip
    await session.execute(insert(reading_minutes), [row])
    with pytest.raises(IntegrityError):
        await session.execute(insert(reading_minutes), [row])


async def test_reading_minutes_indexes(session):
    defs = dict(
        (await session.execute(
            text("SELECT indexname, indexdef FROM pg_indexes WHERE tablename = 'reading_minutes'")
        )).all()
    )  # fmt: skip
    assert "USING brin (minute)" in defs["ix_reading_minutes_minute_brin"]
    assert "(vehicle_id, signal_id, minute)" in defs["ix_reading_minutes_vehicle_signal_minute"]

"""Phase 3 step 6, the PRD exit gate: forecasts match hand-calculated cases.

Each case in tests/data/maintenance_cases.yaml has its working written out for Wisdom to
check; the engine must agree to the km and to the day. Property tests guard the shape
of the logic beyond the cases.
"""

from datetime import date, timedelta
from pathlib import Path

import pytest
import yaml

from app.services.maintenance import (
    LastService,
    add_months,
    forecast_all,
    forecast_item,
    km_since_service,
)
from app.services.service_rules import ResolvedRule, WearFactor, resolve

DATA = yaml.safe_load(
    (Path(__file__).parent / "data" / "maintenance_cases.yaml").read_text(encoding="utf-8")
)
TODAY = DATA["today"]


def run_case(case: dict):
    rule = ResolvedRule("engine_oil", "Test item", case["rule"]["km"], case["rule"]["months"],
                        None, "default", False)  # fmt: skip
    last = None
    if case["last"]:
        last = LastService(case["last"]["done_on"], case["last"]["odometer_km"],
                           case["last"].get("before_install", False))  # fmt: skip
    wear = WearFactor(case["wear_factor"], ("test",)) if "wear_factor" in case else None
    return forecast_item(
        rule,
        last,
        today=TODAY,
        estimate_km=case["estimate_km"],
        km_per_day=case["km_per_day"],
        km_from_trips_since=case.get("km_since_from_trips"),
        wear=wear,
    )


@pytest.mark.parametrize("case", DATA["cases"], ids=[c["name"] for c in DATA["cases"]])
def test_hand_calculated_case(case):
    f = run_case(case)
    for key, want in case["expect"].items():
        assert getattr(f, key) == want, f"{key}: got {getattr(f, key)!r}, want {want!r}"


def test_there_are_enough_cases_covering_every_status():
    statuses = {c["expect"]["status"] for c in DATA["cases"]}
    assert statuses == {"unknown", "ok", "soon", "due", "overdue"}
    assert len(DATA["cases"]) >= 10


# ---------------------------------------------------------------- pieces


@pytest.mark.parametrize(
    "d,months,want",
    [
        (date(2026, 8, 31), 3, date(2026, 11, 30)),
        (date(2026, 1, 31), 1, date(2026, 2, 28)),
        (date(2027, 11, 30), 3, date(2028, 2, 29)),  # leap year
        (date(2026, 10, 15), 3, date(2027, 1, 15)),  # across the year end
        (date(2026, 6, 1), 6, date(2026, 12, 1)),
    ],
)
def test_add_months(d, months, want):
    assert add_months(d, months) == want


def test_km_since_service_sources():
    with_odo = LastService(date(2026, 10, 1), 100_000)
    assert km_since_service(with_odo, 103_800, None) == 3_800
    no_odo_after = LastService(date(2026, 10, 1), None)
    assert km_since_service(no_odo_after, 103_800, 1_450.0) == 1_450.0
    no_odo_before = LastService(date(2026, 6, 1), None, before_install=True)
    assert km_since_service(no_odo_before, 103_800, 1_450.0) is None
    assert km_since_service(with_odo, None, 900.0) == 900.0  # no estimate yet: trips since


# ---------------------------------------------------------------- properties


def _oil(km_per_day=40.0, estimate=103_000):
    rule = resolve(None, None, None, "mineral")["engine_oil"]
    return forecast_item(rule, LastService(date(2026, 10, 1), 100_000), today=TODAY,
                         estimate_km=estimate, km_per_day=km_per_day)  # fmt: skip


def test_more_km_never_makes_it_due_later():
    dates = [_oil(estimate=100_000 + km).due_date for km in range(0, 7000, 250)]
    assert all(b <= a for a, b in zip(dates, dates[1:], strict=False))


def test_driving_more_per_day_never_makes_it_due_later():
    dates = [_oil(km_per_day=k).due_date for k in (0, 5, 20, 50, 100, 300)]
    assert all(b <= a for a, b in zip(dates, dates[1:], strict=False))


def test_a_new_service_record_resets_the_item():
    rule = resolve(None, None, None, "mineral")["engine_oil"]
    old = forecast_item(rule, LastService(date(2026, 7, 1), 90_000), today=TODAY,
                        estimate_km=96_000, km_per_day=40)  # fmt: skip
    assert old.status == "overdue"
    new = forecast_item(rule, LastService(TODAY, 96_000), today=TODAY,
                        estimate_km=96_000, km_per_day=40)  # fmt: skip
    assert new.status == "ok" and new.remaining_km == 5000
    assert new.due_date == min(add_months(TODAY, 3), TODAY + timedelta(days=125))


def test_wear_factor_only_shortens_km():
    rule = resolve(None, None, None, None)["brake_inspection"]
    last = LastService(date(2026, 11, 1), 50_000)
    plain = forecast_item(rule, last, today=TODAY, estimate_km=51_000, km_per_day=20)
    hard = forecast_item(rule, last, today=TODAY, estimate_km=51_000, km_per_day=20,
                         wear=WearFactor(0.8, ("harsh_braking: ...",)))  # fmt: skip
    assert hard.interval_km == 8000 and plain.interval_km == 10000
    assert hard.remaining_km == plain.remaining_km - 2000
    assert hard.interval_months == plain.interval_months  # time interval untouched
    assert any("harsh_braking" in r for r in hard.reasons)


def test_forecast_all_lists_every_item_most_urgent_first():
    resolved = resolve("Toyota", "Corolla", "2ZR-FE", None)
    last = {
        "engine_oil": LastService(date(2026, 7, 1), 90_000),  # overdue
        "battery_check": LastService(date(2026, 6, 1), 85_000),  # soon (2026-12-01)
        "tyre_rotation": LastService(date(2026, 11, 1), 99_000),  # ok
    }
    out = forecast_all(resolved, last, today=TODAY, estimate_km=100_000, km_per_day=30)
    assert len(out) == 10
    assert [f.status for f in out[:3]] == ["overdue", "soon", "ok"]
    assert [f.item for f in out[:3]] == ["engine_oil", "battery_check", "tyre_rotation"]
    assert all(f.status == "unknown" for f in out[3:])  # no history: we ask (Q2)
    oil = out[0]
    assert oil.base_interval_km == 5000  # unknown oil type -> mineral, the shorter (Q1)

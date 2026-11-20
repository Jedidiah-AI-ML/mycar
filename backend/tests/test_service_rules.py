"""Phase 3 step 2: service rules and wear multipliers (config only, no database)."""

import copy

import pytest
import yaml
from pydantic import ValidationError

from app.services import service_rules as sr
from app.services.service_rules import (
    CONFIG_DIR,
    RulesConfig,
    WearConfig,
    resolve,
    rules,
    wear,
    wear_factors,
)

RAW_RULES = yaml.safe_load((CONFIG_DIR / "service_rules.yaml").read_text(encoding="utf-8"))
RAW_WEAR = yaml.safe_load((CONFIG_DIR / "wear.yaml").read_text(encoding="utf-8"))


def rules_from(data: dict) -> RulesConfig:
    d = copy.deepcopy(data)
    items = {k: sr.Item(**v) for k, v in d.pop("items").items()}
    return RulesConfig(items=items, **d)


# ---------------------------------------------------------------- the shipped config


def test_ten_prd_items_each_with_a_time_interval():
    cfg = rules()
    assert set(cfg.items) == {
        "engine_oil", "tyre_rotation", "brake_inspection", "engine_air_filter", "cabin_filter",
        "spark_plugs", "coolant", "transmission_fluid", "battery_check", "wheel_alignment",
    }  # fmt: skip
    for r in cfg.rules:
        assert r.months > 0
        assert (r.km is None) == (r.item == "battery_check"), r.item  # only battery is time-only


def test_prd_default_intervals_exactly():
    """PRD 5, Layer 1 seed defaults (all SEED - validate)."""
    got = {
        k: (v.km, v.months) for k, v in resolve("Toyota", "Corolla", "2ZR-FE", "mineral").items()
    }
    assert got["engine_oil"] == (5000, 3)
    assert resolve(None, None, None, "full_synthetic")["engine_oil"].km == 7500
    assert resolve(None, None, None, "full_synthetic")["engine_oil"].months == 6
    assert got["tyre_rotation"][0] == 10000
    assert got["brake_inspection"][0] == 10000
    assert got["engine_air_filter"][0] == 10000
    assert got["cabin_filter"][0] == 15000
    assert got["battery_check"] == (None, 6)
    assert got["wheel_alignment"][0] == 10000


def test_unknown_oil_type_uses_the_shorter_interval_and_says_so():
    """Decision Q1: oil type unknown -> mineral (5,000 km / 3 months), flagged as assumed."""
    oil = resolve("Lexus", "RX350", "2GR-FE", None)["engine_oil"]
    assert (oil.km, oil.months, oil.variant, oil.assumed_variant) == (5000, 3, "mineral", True)
    known = resolve("Lexus", "RX350", "2GR-FE", "semi_synthetic")["engine_oil"]
    assert known.variant == "semi_synthetic" and not known.assumed_variant
    assert resolve(None, None, None, "castor_oil")["engine_oil"].variant == "mineral"


# ---------------------------------------------------------------- most specific wins


def test_most_specific_matching_rule_wins():
    data = copy.deepcopy(RAW_RULES)
    data["overrides"] = [
        {"item": "spark_plugs", "make": "Honda", "km": 50000, "months": 40},
        {"item": "spark_plugs", "make": "Honda", "model": "Accord", "km": 70000, "months": 50},
        {"item": "spark_plugs", "make": "Honda", "model": "Accord", "engine": "K24",
         "km": 100000, "months": 72},
    ]  # fmt: skip
    cfg = rules_from(data)

    def plugs(make, model, engine):
        r = resolve(make, model, engine, None, cfg=cfg)["spark_plugs"]
        return r.km, r.scope

    assert plugs("Honda", "Accord", "K24A 2.4L I4") == (100000, "Honda / Accord / K24")
    assert plugs("honda", "ACCORD", "J35 3.5L V6") == (70000, "Honda / Accord")  # case-insensitive
    assert plugs("Honda", "Civic", "R18") == (50000, "Honda")
    assert plugs("Toyota", "Corolla", "2ZR-FE") == (60000, "default")
    assert plugs(None, None, None) == (60000, "default")


# ---------------------------------------------------------------- bad config fails loudly


def _mut_rules(fn):
    data = copy.deepcopy(RAW_RULES)
    fn(data)
    return data


BAD_RULES = {
    "unknown item": lambda d: d["rules"].append({"item": "flux_capacitor", "km": 1, "months": 1}),
    "unknown oil variant": lambda d: d["rules"].append(
        {"item": "engine_oil", "variant": "olive", "km": 1, "months": 1}),
    "variant on an item without variants": lambda d: d["rules"].append(
        {"item": "coolant", "variant": "mineral", "km": 1, "months": 1}),
    "duplicate rule": lambda d: d["rules"].append(dict(d["rules"][0])),
    "missing default": lambda d: d.update(
        rules=[r for r in d["rules"] if r["item"] != "cabin_filter"]),
    "missing default for a variant": lambda d: d.update(
        rules=[r for r in d["rules"] if r.get("variant") != "full_synthetic"]),
    "no months": lambda d: d["rules"][0].pop("months"),
    "negative km": lambda d: d["rules"][0].update(km=-5),
    "override without a make": lambda d: d.update(
        overrides=[{"item": "coolant", "km": 1, "months": 1}]),
    "model without make": lambda d: d.update(
        overrides=[{"item": "coolant", "model": "Camry", "km": 1, "months": 1}]),
    "unknown profile": lambda d: d["rules"][0].update(profile="martian"),
    "unknown field": lambda d: d["rules"][0].update(kms=5000),
}  # fmt: skip


@pytest.mark.parametrize("name", sorted(BAD_RULES))
def test_bad_rules_config_fails_at_load(name):
    with pytest.raises(ValidationError):
        rules_from(_mut_rules(BAD_RULES[name]))


def _mut_wear(fn):
    data = copy.deepcopy(RAW_WEAR)
    fn(data)
    return data


BAD_WEAR = {
    "unknown metric": lambda d: d["multipliers"]["heavy_idle"].update(metric="vibes"),
    "unknown item": lambda d: d["multipliers"]["heavy_idle"].update(items=["wipers"]),
    "bands out of order": lambda d: d["multipliers"]["heavy_idle"]["bands"].reverse(),
    "harder driving, bigger factor": lambda d: d["multipliers"]["heavy_idle"].update(
        bands=[{"above": 0.5, "factor": 0.8}, {"above": 0.6, "factor": 0.9}]),
    "below the floor": lambda d: d["multipliers"]["heavy_idle"].update(
        bands=[{"above": 0.5, "factor": 0.5}]),
    "factor above 1": lambda d: d["multipliers"]["heavy_idle"].update(
        bands=[{"above": 0.5, "factor": 1.2}]),
}  # fmt: skip


@pytest.mark.parametrize("name", sorted(BAD_WEAR))
def test_bad_wear_config_fails_at_load(name):
    with pytest.raises(ValidationError):
        WearConfig(**_mut_wear(BAD_WEAR[name]))


# ---------------------------------------------------------------- wear factors


def test_wear_shipped_config_loads():
    w = wear()
    assert w.floor == 0.7 and w.window_days == 30
    assert set(w.multipliers) == {"harsh_braking", "harsh_acceleration", "heavy_idle"}


def test_gentle_driving_changes_nothing():
    m = {"harsh_brakes_per_100km": 1.0, "harsh_accels_per_100km": 2.0, "idle_share": 0.42}
    assert wear_factors(m, km_in_window=900) == {}


def test_harsh_braking_bands():
    f = wear_factors({"harsh_brakes_per_100km": 7.0}, km_in_window=900)
    assert f["brake_inspection"].factor == pytest.approx(0.9)
    assert "harsh_braking" in f["brake_inspection"].reasons[0]
    f = wear_factors({"harsh_brakes_per_100km": 12.0}, km_in_window=900)
    assert f["brake_inspection"].factor == pytest.approx(0.8)
    assert set(f) == {"brake_inspection"}  # braking doesn't touch oil
    assert wear_factors({"harsh_brakes_per_100km": 5.0}, 900) == {}  # "above", not "at"


def test_multipliers_on_one_item_combine_and_respect_the_floor():
    m = {"harsh_accels_per_100km": 12.0, "idle_share": 0.7}
    oil = wear_factors(m, km_in_window=900)["engine_oil"]
    assert oil.factor == pytest.approx(0.9 * 0.8)  # 0.72, just above the floor
    assert len(oil.reasons) == 2
    harsher = WearConfig(**_mut_wear(lambda d: d.update(floor=0.75)))
    assert wear_factors(m, 900, cfg=harsher)["engine_oil"].factor == pytest.approx(0.75)


def test_too_little_driving_gives_no_multiplier():
    m = {"harsh_brakes_per_100km": 30.0, "idle_share": 0.9}
    assert wear_factors(m, km_in_window=40) == {}  # 40 km < min_km_in_window (100)

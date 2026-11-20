"""Service intervals and wear multipliers, from `app/config/service_rules.yaml` and
`app/config/wear.yaml` (both SEED - validate, M1 and M2).

- `rules()` / `wear()`: the validated config (loaded once). A bad file fails at load
  time, with the reason, never halfway through a forecast.
- `resolve(make, model, engine, oil_type)`: which interval applies to each item for
  this car. The most specific matching rule wins (make+model+engine > make+model > make
  > default). An unknown oil type uses `mineral`, the shorter interval (decision Q1).
- `wear_factors(metrics, km_in_window)`: how much the car's driving shortens each item's
  km interval.

Pure functions over config: no database (the DB copy of the rules is synced in step 3).
"""

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

CONFIG_DIR = Path(__file__).resolve().parents[1] / "config"
DEFAULT_PROFILE = "ng_severe"
UNKNOWN_OIL_VARIANT = "mineral"  # decision Q1: the shorter interval when we don't know


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


# ---------------------------------------------------------------- service rules


class Item(_Strict):
    name: str
    variants: tuple[str, ...] = ()


class Rule(_Strict):
    item: str
    variant: str | None = None
    km: int | None = Field(default=None, gt=0)
    months: int = Field(gt=0)
    profile: str = DEFAULT_PROFILE
    make: str | None = None
    model: str | None = None
    engine: str | None = None

    @model_validator(mode="after")
    def _scope_is_a_chain(self) -> "Rule":
        # model needs make; engine needs make + model.
        if (self.model and not self.make) or (self.engine and not (self.make and self.model)):
            raise ValueError(f"{self.item}: scope must be make, make+model or make+model+engine")
        return self

    @property
    def specificity(self) -> int:
        return sum(x is not None for x in (self.make, self.model, self.engine))

    @property
    def scope(self) -> str:
        parts = [p for p in (self.make, self.model, self.engine) if p]
        return " / ".join(parts) or "default"

    def matches(self, make: str | None, model: str | None, engine: str | None) -> bool:
        def eq(rule_val, car_val):
            return rule_val is None or (car_val or "").strip().lower() == rule_val.lower()

        engine_ok = self.engine is None or self.engine.lower() in (engine or "").lower()
        return eq(self.make, make) and eq(self.model, model) and engine_ok


class DueSoon(_Strict):
    pct_of_interval: float = Field(gt=0, lt=100)
    days: int = Field(gt=0)


class RulesConfig(_Strict):
    version: int
    profiles: dict[str, str]
    due_soon: DueSoon
    items: dict[str, Item]
    rules: tuple[Rule, ...]
    overrides: tuple[Rule, ...] = ()

    @model_validator(mode="after")
    def _consistent(self) -> "RulesConfig":
        seen = set()
        for r in (*self.rules, *self.overrides):
            if r.item not in self.items:
                raise ValueError(f"rule for unknown item {r.item!r}")
            if r.profile not in self.profiles:
                raise ValueError(f"{r.item}: unknown profile {r.profile!r}")
            variants = self.items[r.item].variants
            if variants and r.variant not in variants:
                raise ValueError(f"{r.item}: variant must be one of {list(variants)}")
            if not variants and r.variant is not None:
                raise ValueError(f"{r.item} has no variants")
            key = (r.item, r.variant, r.profile, r.make, r.model, r.engine)
            if key in seen:
                raise ValueError(f"duplicate rule {key}")
            seen.add(key)
        for r in self.overrides:
            if r.specificity == 0:
                raise ValueError(f"override for {r.item} must name at least a make")
        # Every item has a default rule (for every variant): any car gets an answer.
        for key, item in self.items.items():
            for v in item.variants or (None,):
                if not any(r.item == key and r.variant == v and r.specificity == 0
                           for r in self.rules):  # fmt: skip
                    raise ValueError(f"no default rule for {key}" + (f" ({v})" if v else ""))
        if UNKNOWN_OIL_VARIANT not in self.items["engine_oil"].variants:
            raise ValueError(
                "engine_oil needs the 'mineral' variant (used when oil type is unknown)"
            )
        return self


@dataclass(frozen=True)
class ResolvedRule:
    item: str
    name: str
    km: int | None
    months: int
    variant: str | None
    scope: str  # "default" or e.g. "Toyota / Corolla"
    assumed_variant: bool  # oil type unknown -> the shorter interval was assumed


def load_rules(path: Path = CONFIG_DIR / "service_rules.yaml") -> RulesConfig:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    items = {k: Item(**v) for k, v in data.pop("items").items()}
    return RulesConfig(items=items, **data)


@lru_cache
def rules() -> RulesConfig:
    return load_rules()


def resolve(
    make: str | None,
    model: str | None,
    engine: str | None,
    oil_type: str | None,
    cfg: RulesConfig | None = None,
    profile: str = DEFAULT_PROFILE,
) -> dict[str, ResolvedRule]:
    cfg = cfg or rules()
    out = {}
    for key, item in cfg.items.items():
        variant, assumed = None, False
        if item.variants:
            variant = oil_type if oil_type in item.variants else UNKNOWN_OIL_VARIANT
            assumed = oil_type not in item.variants
        candidates = [
            r for r in (*cfg.overrides, *cfg.rules)
            if r.item == key and r.variant == variant and r.profile == profile
            and r.matches(make, model, engine)
        ]  # fmt: skip
        best = max(candidates, key=lambda r: r.specificity)
        out[key] = ResolvedRule(key, item.name, best.km, best.months, variant, best.scope, assumed)
    return out


# ---------------------------------------------------------------- wear multipliers


class Band(_Strict):
    above: float = Field(ge=0)
    factor: float = Field(gt=0, le=1)


class Multiplier(_Strict):
    metric: str
    items: tuple[str, ...]
    bands: tuple[Band, ...]

    @field_validator("bands")
    @classmethod
    def _ascending(cls, bands: tuple[Band, ...]) -> tuple[Band, ...]:
        if not bands:
            raise ValueError("at least one band")
        if [b.above for b in bands] != sorted(b.above for b in bands):
            raise ValueError("bands must be in ascending order of `above`")
        if [b.factor for b in bands] != sorted((b.factor for b in bands), reverse=True):
            raise ValueError("harder driving must never give a larger factor")
        return bands


METRICS = {"harsh_brakes_per_100km", "harsh_accels_per_100km", "idle_share"}


class WearConfig(_Strict):
    version: int
    window_days: int = Field(gt=0)
    floor: float = Field(gt=0, le=1)
    min_km_in_window: float = Field(ge=0)
    multipliers: dict[str, Multiplier]

    @model_validator(mode="after")
    def _consistent(self) -> "WearConfig":
        known_items = set(rules().items)
        for name, m in self.multipliers.items():
            if m.metric not in METRICS:
                raise ValueError(f"{name}: unknown metric {m.metric!r} (known: {sorted(METRICS)})")
            if unknown := set(m.items) - known_items:
                raise ValueError(f"{name}: unknown items {sorted(unknown)}")
            if min(b.factor for b in m.bands) < self.floor:
                raise ValueError(f"{name}: a band goes below the floor {self.floor}")
        return self


def load_wear(path: Path = CONFIG_DIR / "wear.yaml") -> WearConfig:
    return WearConfig(**yaml.safe_load(path.read_text(encoding="utf-8")))


@lru_cache
def wear() -> WearConfig:
    return load_wear()


@dataclass(frozen=True)
class WearFactor:
    factor: float  # multiply the km interval by this (1.0 = no change)
    reasons: tuple[str, ...]  # e.g. "harsh_braking: 7.2 per 100 km (above 5)"


def wear_factors(
    metrics: dict[str, float], km_in_window: float, cfg: WearConfig | None = None
) -> dict[str, WearFactor]:
    """Per item, the combined factor from the car's driving in the trailing window.

    Items not affected (or too little driving to judge) get factor 1.0.
    """
    cfg = cfg or wear()
    factors: dict[str, float] = {}
    reasons: dict[str, list[str]] = {}
    if km_in_window >= cfg.min_km_in_window:
        for name, m in cfg.multipliers.items():
            value = metrics.get(m.metric)
            if value is None:
                continue
            hit = [b for b in m.bands if value > b.above]
            if not hit:
                continue
            band = hit[-1]  # the highest band the metric is above
            for item in m.items:
                factors[item] = factors.get(item, 1.0) * band.factor
                reasons.setdefault(item, []).append(
                    f"{name}: {value:g} {m.metric} (above {band.above:g}) x{band.factor:g}"
                )
    return {
        item: WearFactor(max(f, cfg.floor), tuple(reasons[item])) for item, f in factors.items()
    }


# ---------------------------------------------------------------- database copy


async def sync_to_db(session, cfg: RulesConfig | None = None) -> tuple[int, int]:
    """Copy the config into `service_items` / `service_rules`. Idempotent.

    Items are upserted and never deleted (service records point at them). Rules are
    replaced as a whole: nothing points at them, and the config is the source of truth.
    Returns (items, rules) in the config. The caller commits.
    """
    from sqlalchemy import delete
    from sqlalchemy.dialects.postgresql import insert

    from app.models import ServiceItem, ServiceRule

    cfg = cfg or rules()
    stmt = insert(ServiceItem).values([{"key": k, "name": i.name} for k, i in cfg.items.items()])
    await session.execute(
        stmt.on_conflict_do_update(
            index_elements=[ServiceItem.key], set_={"name": stmt.excluded.name}
        )
    )
    await session.execute(delete(ServiceRule))
    all_rules = (*cfg.rules, *cfg.overrides)
    await session.execute(
        insert(ServiceRule),
        [
            {"item_key": r.item, "profile": r.profile, "variant": r.variant, "make": r.make,
             "model": r.model, "engine": r.engine, "interval_km": r.km,
             "interval_months": r.months}
            for r in all_rules
        ],
    )  # fmt: skip
    return len(cfg.items), len(all_rules)

"""Maintenance forecasts: pure functions, no database (Phase 3 step 6).

For each service item, from its last service, the car's odometer estimate and km/day:

- **km side:** km remaining = (km interval × wear factor) − km since the service.
  Turned into a date with km/day, rounded **up** to whole days (never promise a day too
  late). A car that isn't moving (0 km/day) has no km date.
- **time side:** service date + interval months, the way a service book counts them
  (31 Aug + 3 months = 30 Nov).
- **whichever comes first** is the due date.
- **status:** overdue (past on either side) > due (today, or exactly 0 km left) >
  soon (within 10 % of the interval or 14 days; SEED M3) > ok. No history -> unknown
  (decision Q2: we ask rather than guess).

"Km since the service" depends on what we know (`km_since_service`):
- the service has an odometer value and we have an estimate: estimate − that value;
- no odometer value, but the service was after install: the trips' distance since then;
- otherwise (pre-install history with no odometer): unknown, and only time counts.

Wear factors shorten km intervals only (app/config/wear.yaml). Golden cases with the
working shown: backend/tests/data/maintenance_cases.yaml.
"""

import calendar
import math
from dataclasses import dataclass, field
from datetime import date

from app.services.service_rules import ResolvedRule, WearFactor, rules

STATUS_ORDER = ("overdue", "due", "soon", "ok", "unknown")  # most urgent first


@dataclass(frozen=True)
class LastService:
    done_on: date
    odometer_km: int | None = None
    before_install: bool = False


@dataclass(frozen=True)
class ItemForecast:
    item: str
    name: str
    status: str  # overdue / due / soon / ok / unknown
    interval_km: int | None  # after the wear factor
    base_interval_km: int | None
    interval_months: int
    wear_factor: float
    km_since: float | None
    remaining_km: int | None
    due_date: date | None
    due_by: str | None  # km / time
    days_left: int | None
    last_service: date | None
    reasons: tuple[str, ...] = field(default_factory=tuple)


def add_months(d: date, months: int) -> date:
    """Calendar months, clamped to the month's last day (31 Aug + 3 months = 30 Nov)."""
    m = d.month - 1 + months
    year, month = d.year + m // 12, m % 12 + 1
    return date(year, month, min(d.day, calendar.monthrange(year, month)[1]))


def km_since_service(
    last: LastService, estimate_km: int | None, km_from_trips_since: float | None
) -> float | None:
    if last.odometer_km is not None and estimate_km is not None:
        return float(estimate_km - last.odometer_km)
    if not last.before_install and km_from_trips_since is not None:
        return km_from_trips_since
    return None


def forecast_item(
    rule: ResolvedRule,
    last: LastService | None,
    *,
    today: date,
    estimate_km: int | None,
    km_per_day: float,
    km_from_trips_since: float | None = None,
    wear: WearFactor | None = None,
    soon_pct: float | None = None,
    soon_days: int | None = None,
) -> ItemForecast:
    ds = rules().due_soon
    soon_pct = ds.pct_of_interval if soon_pct is None else soon_pct
    soon_days = ds.days if soon_days is None else soon_days
    factor = wear.factor if wear else 1.0
    interval_km = round(rule.km * factor) if rule.km else None
    reasons = list(wear.reasons) if wear and factor != 1.0 else []
    base = dict(item=rule.item, name=rule.name, interval_km=interval_km, base_interval_km=rule.km,
                interval_months=rule.months, wear_factor=factor)  # fmt: skip

    if last is None:
        reasons.append("no service history yet: tell us when it was last done")
        return ItemForecast(**base, status="unknown", km_since=None, remaining_km=None,
                            due_date=None, due_by=None, days_left=None, last_service=None,
                            reasons=tuple(reasons))  # fmt: skip

    # Time side.
    due_time = add_months(last.done_on, rule.months)
    candidates = [(due_time, "time")]

    # Km side.
    since = remaining = None
    if interval_km is not None:
        since = km_since_service(last, estimate_km, km_from_trips_since)
        if since is None:
            reasons.append("km since the last service unknown (no odometer then): by time only")
        else:
            remaining = round(interval_km - since)
            if km_per_day > 0:
                days = math.ceil(remaining / km_per_day)
                candidates.append((date.fromordinal(today.toordinal() + days), "km"))

    due_date, due_by = min(candidates, key=lambda c: (c[0], c[1] != "km"))
    days_left = (due_date - today).days

    if (remaining is not None and remaining < 0) or days_left < 0:
        status = "overdue"
    elif remaining == 0 or days_left == 0:
        status = "due"
    elif (remaining is not None and remaining <= soon_pct / 100 * interval_km) or (
        days_left <= soon_days
    ):
        status = "soon"
    else:
        status = "ok"

    if due_by == "time":
        reasons.append(f"by time: {rule.months} months after {last.done_on.isoformat()}")
    else:
        reasons.append(f"by km: {remaining:,} km left at {km_per_day:g} km/day")
    return ItemForecast(**base, status=status, km_since=since, remaining_km=remaining,
                        due_date=due_date, due_by=due_by, days_left=days_left,
                        last_service=last.done_on, reasons=tuple(reasons))  # fmt: skip


def forecast_all(
    resolved: dict[str, ResolvedRule],
    last_by_item: dict[str, LastService],
    *,
    today: date,
    estimate_km: int | None,
    km_per_day: float,
    km_from_trips_since: dict[str, float] | None = None,
    wear: dict[str, WearFactor] | None = None,
) -> list[ItemForecast]:
    """Every item, most urgent first (then soonest due)."""
    out = [
        forecast_item(
            rule, last_by_item.get(key), today=today, estimate_km=estimate_km,
            km_per_day=km_per_day, km_from_trips_since=(km_from_trips_since or {}).get(key),
            wear=(wear or {}).get(key),
        )
        for key, rule in resolved.items()
    ]  # fmt: skip
    return sorted(out, key=lambda f: (STATUS_ORDER.index(f.status), f.due_date or date.max, f.item))

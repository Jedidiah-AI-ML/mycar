"""Phase 3 exit gate (docs/plans/phase3-plan.md step 8): maintenance forecasts end to end.

    docker compose up -d db
    cd simulator
    uv run python ../scripts/phase3_gate.py            # ~20-30 min

Local only (nothing touches Neon or Railway). Reuses the Phase 2 gate's plumbing:
a throwaway `autocare_gate3` database and its own API on port 8013 with the simulation
clock on.

1. Two Toyota Corollas installed 60 days ago at 120,000 km: one **gentle** driver, one
   **hard** driver (brakes past 0.35 g often). 60 days of Lagos driving each, ending
   today, uploaded over HTTP with Lagos coverage (outages, retries, late batches).
2. Service history entered as an owner would, through the API: every item done at
   install; an oil change on day 30 with the true odometer; a cabin-filter record on
   day 40 with a mistyped odometer (3,000 km low), which must be flagged and not trusted.
3. `GET /vehicles/{id}/maintenance` is compared with forecasts worked out from the
   **simulator's true distances** using the hand-checked rules (the golden cases):
   - odometer estimate within 1 % of the true distance driven since install
   - km/day within 2 % of the true trailing-30-day mean
   - every item's km remaining within 1 % of its interval
   - the hard car's brake/oil km intervals shortened by exactly the wear.yaml factors for
     the wear metrics it reports, and its harsh-brake rate above the top band
Writes `ml/reports/phase3_gate.md`. Exit code 1 if the gate fails.
"""

import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))
sys.path.insert(0, str(REPO / "simulator"))

import phase2_gate as g  # noqa: E402
import yaml  # noqa: E402

from autocare_sim.device import Device  # noqa: E402
from autocare_sim.network import Network  # noqa: E402
from autocare_sim.run import LAGOS, simulate  # noqa: E402

g.GATE_DB, g.PORT = "autocare_gate3", 8013
g.BASE = f"http://127.0.0.1:{g.PORT}"


# phase2_gate.psql's `db` default was fixed when it was defined: always pass ours.
def psql(sql: str) -> str:
    return g.psql(sql, db=g.GATE_DB)


def scalar(sql: str) -> int:
    return int(psql(sql) or 0)


DAY_MS = 86_400_000
DAYS = int(sys.argv[sys.argv.index("--days") + 1]) if "--days" in sys.argv else 60
INSTALL_KM = 120_000
PID = "toyota_corolla_1_8_2009"
RULES = yaml.safe_load((REPO / "backend/app/config/service_rules.yaml").read_text("utf-8"))
WEAR = yaml.safe_load((REPO / "backend/app/config/wear.yaml").read_text("utf-8"))
CARS = [("gentle", "normal", 31), ("hard", "hard", 32)]


def expected_factor(item: str, wear: dict) -> float:
    """What wear.yaml says this item's km factor should be for these 30-day metrics."""
    if wear["km_in_window"] < WEAR["min_km_in_window"]:
        return 1.0
    f = 1.0
    for m in WEAR["multipliers"].values():
        value = wear.get(m["metric"])
        if item not in m["items"] or value is None:
            continue
        hit = [b for b in m["bands"] if value > b["above"]]
        if hit:
            f *= hit[-1]["factor"]
    return max(f, WEAR["floor"])


def base_interval(item: str, oil: str = "mineral") -> int | None:
    for r in RULES["rules"]:
        if r["item"] == item and r.get("variant") in (None, oil):
            return r["km"]
    raise KeyError(item)


def main() -> int:
    started = time.time()
    tmp = Path(__import__("tempfile").mkdtemp(prefix="phase3_gate_"))
    print("1/6 fresh database + local API ...", flush=True)
    g.reset_database()
    proc = g.start_api(tmp / "api.log")
    try:
        return run(started)
    finally:
        g.stop_api(proc)


def run(started: float) -> int:  # noqa: C901, PLR0915 - a linear script reads best top to bottom
    now = datetime.now(UTC)
    today = now.astimezone(LAGOS).date()
    start = datetime.combine(today - timedelta(days=DAYS), datetime.min.time(), LAGOS)

    print("2/6 owners, cars, devices (via the API) ...", flush=True)
    admin_email = "gate3-admin@example.com"
    g.api(
        "POST",
        "/auth/register",
        {
            "email": admin_email,
            "password": g.PASSWORD,
            "display_name": "admin",
            "consents": ["service_processing"],
        },
    )
    psql(f"UPDATE users SET is_admin = true WHERE email = '{admin_email}'")
    admin = g.api("POST", "/auth/login", {"email": admin_email, "password": g.PASSWORD})
    admin = admin["access_token"]
    cars = []
    for name, driver, seed in CARS:
        email = f"gate3-{name}@example.com"
        g.api(
            "POST",
            "/auth/register",
            {
                "email": email,
                "password": g.PASSWORD,
                "display_name": name,
                "consents": ["service_processing"],
            },
        )
        tok = g.api("POST", "/auth/login", {"email": email, "password": g.PASSWORD})["access_token"]
        v = g.api(
            "POST",
            "/vehicles",
            {
                "make": "Toyota",
                "model": "Corolla",
                "year": 2010,
                "engine": "2ZR-FE 1.8L I4",
                "odometer_at_install_km": INSTALL_KM,
                "installed_at": start.date().isoformat(),
            },
            tok,
        )
        dev = g.api("POST", "/admin/devices", {}, admin)
        g.api("POST", f"/vehicles/{v['id']}/device",
              {"device_id": dev["id"], "claim_code": dev["claim_code"]}, tok)  # fmt: skip
        cars.append({"name": name, "email": email, "driver": driver, "seed": seed, "token": tok,
                     "vid": v["id"],
                     "device_id": dev["id"], "secret": dev["token"].split(".", 1)[1]})  # fmt: skip

    print(f"3/6 simulating {DAYS} days x 2 cars (gentle, hard) and uploading ...", flush=True)
    n_requests = 0
    for car in cars:
        tl = simulate(PID, days=DAYS, seed=car["seed"], start=start, driver=car["driver"])
        tl.trips = [t for t in tl.trips if t.end < now - timedelta(minutes=10)]
        t0, t1 = int(tl.trips[0].t_ms[0]), int(tl.trips[-1].t_ms[-1])
        sink = g.RecordingSink(g.BASE, car["device_id"], car["secret"], sim_clock=True)
        net = Network.lagos(t0, t1 + 3 * DAY_MS, seed=car["seed"])
        tt = time.time()
        run_ = Device(car["device_id"], tl, seed=car["seed"], network=net, sink=sink).run()
        n_requests += len(sink.sent)
        car.update(tl=tl, run=run_)
        print(f"    {car['name']:6s} {len(tl.trips):3d} trips, "
              f"{sum(t.out.distance_km for t in tl.trips):7.1f} km, {len(run_.produced):5d} batches, "
              f"{time.time() - tt:.0f} s", flush=True)  # fmt: skip

    print("4/6 waiting for trip assembly + summaries ...", flush=True)
    for _ in range(600):
        busy = scalar("SELECT count(*) FROM jobs WHERE status IN ('queued','running','failed') "
                        "AND run_at < now() + interval '1 minute'")  # fmt: skip
        if busy == 0:
            break
        time.sleep(2)

    print("5/6 entering service history (as an owner would) ...", flush=True)

    def true_odo(tl, at: datetime) -> float:
        return INSTALL_KM + sum(t.out.distance_km for t in tl.trips if t.start < at)

    for car in cars:
        # Access tokens last 30 min and the upload takes longer: log in again, the way the
        # owner would come back to the app.
        car["token"] = g.api("POST", "/auth/login", {"email": car["email"], "password": g.PASSWORD})[
            "access_token"
        ]
        tl, h, vid = car["tl"], car["token"], car["vid"]
        for item in RULES["items"]:
            g.api("POST", f"/vehicles/{vid}/service-records",
                  {"item_key": item, "done_on": start.date().isoformat(),
                   "odometer_km": INSTALL_KM}, h)  # fmt: skip
        oil_day = start + timedelta(days=DAYS // 2)
        oil_odo = round(true_odo(tl, oil_day))
        r = g.api("POST", f"/vehicles/{vid}/service-records",
                  {"item_key": "engine_oil", "done_on": oil_day.date().isoformat(),
                   "odometer_km": oil_odo}, h)  # fmt: skip
        car["oil"] = (oil_day, oil_odo, r["odometer_check"])
        cab_day = start + timedelta(days=DAYS * 2 // 3)
        cab_odo = round(true_odo(tl, cab_day)) - 3000  # mistyped
        r = g.api("POST", f"/vehicles/{vid}/service-records",
                  {"item_key": "cabin_filter", "done_on": cab_day.date().isoformat(),
                   "odometer_km": cab_odo}, h)  # fmt: skip
        car["cabin"] = (cab_day, cab_odo, r["odometer_check"])
        car["queried_at"] = datetime.now(UTC)  # the API's "now" for km/day and wear
        car["m"] = g.api("GET", f"/vehicles/{vid}/maintenance", token=h)

    print("6/6 checking against the simulator's true distances ...", flush=True)
    checks, rows = [], []
    for car in cars:
        tl, m = car["tl"], car["m"]
        truth_total = sum(t.out.distance_km for t in tl.trips)
        # Trusted anchor: the oil change half-way (agreed with the estimate then).
        oil_day, oil_odo, oil_check = car["oil"]
        truth_est = oil_odo + sum(t.out.distance_km for t in tl.trips if t.start >= oil_day)
        api_est = m["odometer"]["estimate_km"]
        since_install = truth_est - INSTALL_KM
        # The API's rule, at the moment it was asked (a long run, or a sleeping PC, can put
        # that days after the simulated data ends): the last 30 days, or since install if
        # that's shorter. Days after the last trip count as parked days.
        q = car["queried_at"]
        window = min((q - start).total_seconds() / 86_400, 30.0)
        kmw = sum(t.out.distance_km for t in tl.trips if t.start >= q - timedelta(days=window))
        truth_kmday = kmw / window
        api_kmday = m["usage"]["km_per_day"]
        trips_db = scalar(f"SELECT count(*) FROM trips WHERE vehicle_id = '{car['vid']}' "
                            "AND summarised_at IS NOT NULL")  # fmt: skip
        checks += [
            (f"{car['name']}: every trip assembled and summarised", trips_db == len(tl.trips),
             f"{trips_db} / {len(tl.trips)}"),
            (f"{car['name']}: oil-change odometer agreed (trusted anchor)",
             oil_check is not None and not oil_check["flagged"] and
             m["odometer"]["anchor_source"] == "service", f"{oil_check}"),
            (f"{car['name']}: mistyped cabin-filter odometer flagged",
             car["cabin"][2] is not None and car["cabin"][2]["flagged"],
             f"diff {car['cabin'][2]['diff_km'] if car['cabin'][2] else None} km"),
            (f"{car['name']}: odometer estimate within 1 % of true distance since install",
             abs(api_est - truth_est) <= 0.01 * since_install,
             f"API {api_est:,} vs true {truth_est:,.0f} ({since_install:,.0f} km driven)"),
            (f"{car['name']}: km/day within 2 % of the true 30-day mean",
             abs(api_kmday - truth_kmday) <= 0.02 * truth_kmday + 0.05,
             f"API {api_kmday} vs true {truth_kmday:.1f}"),
        ]  # fmt: skip
        last_odo = {i: INSTALL_KM for i in RULES["items"]}
        last_odo["engine_oil"] = oil_odo
        last_odo["cabin_filter"] = car["cabin"][1]
        for it in m["items"]:
            base = base_interval(it["item"])
            if base is None:  # battery: time only
                continue
            factor = expected_factor(it["item"], m["wear"])
            want_interval = round(base * factor)
            want_left = round(want_interval - (truth_est - last_odo[it["item"]]))
            ok = (it["interval_km"] == want_interval
                  and abs(it["remaining_km"] - want_left) <= 0.01 * base)  # fmt: skip
            rows.append({"car": car["name"], "item": it["item"], "status": it["status"],
                         "interval": it["interval_km"], "factor": it["wear_factor"],
                         "left_api": it["remaining_km"], "left_true": want_left,
                         "due": it["due_date"], "by": it["due_by"]})  # fmt: skip
            checks.append((f"{car['name']}: {it['item']} km left within 1 % of interval", ok,
                           f"API {it['remaining_km']:,} vs true {want_left:,}; interval "
                           f"{it['interval_km']:,} (want {want_interval:,})"))  # fmt: skip
        car["truth"] = {"total_km": truth_total, "est": truth_est, "kmday": truth_kmday}

    gentle, hard = cars
    gb = next(i for i in gentle["m"]["items"] if i["item"] == "brake_inspection")
    hb = next(i for i in hard["m"]["items"] if i["item"] == "brake_inspection")
    top_band = max(b["above"] for b in WEAR["multipliers"]["harsh_braking"]["bands"])
    checks += [
        ("hard driver: harsh braking above the top band",
         (hard["m"]["wear"]["harsh_brakes_per_100km"] or 0) > top_band,
         f"{hard['m']['wear']['harsh_brakes_per_100km']} per 100 km (top band {top_band})"),
        ("gentle driver: no brake multiplier", gb["wear_factor"] == 1.0 and gb["interval_km"] == 10000,
         f"factor {gb['wear_factor']}, interval {gb['interval_km']:,}"),
        ("hard driver: brake inspection comes due sooner by the configured factor",
         hb["interval_km"] == round(10000 * expected_factor("brake_inspection", hard["m"]["wear"]))
         and hb["wear_factor"] < 1, f"factor {hb['wear_factor']}, interval {hb['interval_km']:,}"),
    ]  # fmt: skip
    dead = scalar("SELECT count(*) FROM jobs WHERE status = 'dead'")
    checks.append(("no dead jobs", dead == 0, str(dead)))
    passed = all(ok for _, ok, _ in checks)
    write_report(cars, checks, rows, passed, n_requests, time.time() - started)
    for name, ok, detail in checks:
        if not ok or "km left" not in name:
            print(f"  [{'PASS' if ok else 'FAIL'}] {name}: {detail}")
    n_items = sum(1 for n, _, _ in checks if "km left" in n)
    n_items_ok = sum(1 for n, ok, _ in checks if "km left" in n and ok)
    print(f"  item checks: {n_items_ok}/{n_items} within 1 %")
    print(f"\n{'GATE PASSED' if passed else 'GATE FAILED'}  ->  ml/reports/phase3_gate.md")
    return 0 if passed else 1


def write_report(cars, checks, rows, passed, n_requests, wall_s) -> None:
    L = []
    w = L.append
    w("# Phase 3 exit gate: maintenance forecasts end to end\n")
    w(f"Generated by `scripts/phase3_gate.py` on {datetime.now():%Y-%m-%d %H:%M}. Two Toyota "
      f"Corollas installed {DAYS} days ago at {INSTALL_KM:,} km, simulated Lagos driving, "
      "uploaded over HTTP to a local API; forecasts compared with the simulator's **true** "
      "distances. Local Docker stack only.\n")  # fmt: skip
    w(
        f"**Result: {'PASSED' if passed else 'FAILED'}**: {sum(ok for _, ok, _ in checks)} of "
        f"{len(checks)} checks. {n_requests:,} upload requests, {wall_s / 60:.1f} min.\n"
    )
    w("## The two cars\n")
    w(
        "| Car | Trips | True km | Estimate (API / true) | km/day (API / true) | "
        "Harsh brakes /100 km | Idle share |"
    )
    w("|---|---|---|---|---|---|---|")
    for c in cars:
        m, t = c["m"], c["truth"]
        w(
            f"| {c['name']} | {len(c['tl'].trips)} | {t['total_km']:,.0f} | "
            f"{m['odometer']['estimate_km']:,} / {t['est']:,.0f} | {m['usage']['km_per_day']} / "
            f"{t['kmday']:.1f} | {m['wear']['harsh_brakes_per_100km']} | {m['wear']['idle_share']} |"
        )
    w("\n## Checks\n")
    w("| Check | Result | Detail |\n|---|---|---|")
    for name, ok, detail in checks:
        w(f"| {name} | {'✅' if ok else '❌'} | {detail} |")
    w("\n## Forecasts (API vs truth)\n")
    w("| Car | Item | Status | Interval km (factor) | km left: API / true | Due | By |")
    w("|---|---|---|---|---|---|---|")
    for r in rows:
        w(
            f"| {r['car']} | {r['item']} | {r['status']} | {r['interval']:,} (x{r['factor']}) | "
            f"{r['left_api']:,} / {r['left_true']:,} | {r['due']} | {r['by']} |"
        )
    w("\n## Notes\n")
    w(
        "- Service history: every item at install (120,000 km); oil on day 30 with the true "
        "odometer (trusted anchor); cabin filter on day 40 with an odometer typed 3,000 km low, "
        "flagged and not trusted. Its forecast still uses that recorded value, as the rules say."
    )
    w(
        '- "True" forecasts use the golden-case rules (tests/data/maintenance_cases.yaml) with '
        "the simulator's distances in place of the measured ones."
    )
    w(
        "- The hard driver is a simulator persona (`drivers.hard` in simulator/profiles/cycles.yaml, "
        "SEED), braking and accelerating about twice as hard as normal."
    )
    (REPO / "ml/reports/phase3_gate.md").write_text("\n".join(L) + "\n", encoding="utf-8",
                                                    newline="\n")  # fmt: skip


if __name__ == "__main__":
    sys.exit(main())

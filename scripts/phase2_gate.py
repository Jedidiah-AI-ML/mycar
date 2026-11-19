"""Phase 2 exit gate (docs/plans/phase2-plan.md step 10; PRD 13).

    docker compose up -d db
    cd simulator
    uv run python ../scripts/phase2_gate.py            # ~5-10 min

What it does, all locally (nothing touches Neon or Railway):
1. Creates a throwaway `autocare_gate` database in the local Docker Postgres, migrates it,
   and starts its own API on port 8010 (embedded worker on, simulation clock on).
2. Through the real API: 5 owners, 5 cars, an admin who provisions 5 devices, owners
   claim them. Three owners give location consent, two don't.
3. Simulates 7 days x 5 cars with Lagos coverage (outages, lost requests and acks ->
   retries and duplicates, freshest-first reordering), drifting and resetting device
   clocks, and faults on 2 cars. Every upload goes over HTTP, exactly as the firmware will.
4. Replays every batch again, shuffled.
5. Compares what the devices produced with what the database holds, and writes
   `ml/reports/phase2_gate.md`. Exit code 1 if the gate fails.
"""

import argparse
import gzip
import json
import os
import random
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "simulator"))

from autocare_sim.config import catalogue, load_profile, profile_ids  # noqa: E402
from autocare_sim.device import Device  # noqa: E402
from autocare_sim.faults import fault  # noqa: E402
from autocare_sim.network import Network  # noqa: E402
from autocare_sim.outputs.freematics import FreematicsHttpSink  # noqa: E402
from autocare_sim.run import simulate  # noqa: E402

GATE_DB = "autocare_gate"
PG = "autocare:autocare@localhost:5433"
PORT = 8010
BASE = f"http://127.0.0.1:{PORT}"
DAY_MS = 86_400_000
PASSWORD = "gate-run-password-1"  # local throwaway accounts only
CONSENTING = {0, 2, 4}  # car indexes whose owner allows location
FAULTS = {  # fast versions of library faults, so DTCs appear inside one week
    "toyota_corolla_1_8_2009": [fault("stuck_open_thermostat", onset=1, ramp=3)],
    "lexus_rx350_3_5_2010": [fault("vacuum_leak", onset=1, ramp=4)],
}


# ---------------------------------------------------------------- plumbing


def psql(sql: str, db: str = GATE_DB) -> str:
    out = subprocess.run(
        ["docker", "compose", "-f", str(REPO / "docker-compose.yml"), "exec", "-T", "db",
         "psql", "-U", "autocare", "-d", db, "-tAc", sql],
        capture_output=True, text=True, check=True,
    )  # fmt: skip
    return out.stdout.strip()


def scalar(sql: str) -> int:
    return int(psql(sql) or 0)


def api(method: str, path: str, body: dict | None = None, token: str | None = None) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method)  # noqa: S310 - local
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:  # noqa: S310
            raw = r.read()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"{method} {path}: {e.code} {e.read()[:300]!r}") from None


def start_api(log_path: Path) -> subprocess.Popen:
    env = {
        **os.environ,
        "DATABASE_URL": f"postgresql+asyncpg://{PG}/{GATE_DB}",
        "ENVIRONMENT": "development",
        "INGEST_SIM_CLOCK": "true",  # trust X-Sim-Server-Time (refused in production)
        "INGEST_RATE_PER_MIN": "1000000",  # a week replays in minutes; 429s are unit-tested
        "TRIP_ASSEMBLE_DELAY_S": "5",
        "WORKER_MODE": "embedded",
        "LOG_LEVEL": "WARNING",
    }
    proc = subprocess.Popen(
        ["uv", "run", "uvicorn", "app.main:app", "--port", str(PORT), "--log-level", "warning"],
        cwd=REPO / "backend", env=env, stdout=log_path.open("w"), stderr=subprocess.STDOUT,
    )  # fmt: skip
    for _ in range(60):
        try:
            if api("GET", "/health").get("status") == "ok":
                return proc
        except Exception:  # noqa: BLE001 - still starting
            time.sleep(1)
    proc.kill()
    raise RuntimeError(f"API didn't start; see {log_path}")


def reset_database() -> None:
    psql(f'DROP DATABASE IF EXISTS "{GATE_DB}" WITH (FORCE)', db="postgres")
    psql(f'CREATE DATABASE "{GATE_DB}"', db="postgres")
    subprocess.run(
        ["uv", "run", "alembic", "upgrade", "head"], cwd=REPO / "backend", check=True,
        env={**os.environ, "DATABASE_URL": f"postgresql+asyncpg://{PG}/{GATE_DB}"},
        capture_output=True,
    )  # fmt: skip


class RecordingSink(FreematicsHttpSink):
    """Posts over HTTP and remembers every body the server received (for the replay)."""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.sent: list[tuple[str, bytes, int, bool]] = []  # kind, body, sim ms, dup
        self.http_s = 0.0

    def post(self, kind, body, true_ms=None):
        t = time.perf_counter()
        try:
            ack = super().post(kind, body, true_ms)
        finally:
            self.http_s += time.perf_counter() - t
        self.sent.append((kind, body, true_ms, bool(ack.get("dup"))))
        return ack


# ---------------------------------------------------------------- expected counts


def expected_readings(batches: list[dict], criticals: list[dict]) -> int:
    cat = catalogue()

    def ok(name, v):
        return name in cat and cat[name]["min"] <= v <= cat[name]["max"]

    n = 0
    for b in batches:
        for name, rows in b["s"].items():
            n += sum(ok(name, v) for _, v in rows)
        for ev in b["ev"]:
            if ev["type"] == "crank":
                n += sum(ok("crank_v", v) for v in ev["volts"])
    for c in criticals:
        n += sum(ok(k, v) for k, v in c["values"].items())
    return n


# ---------------------------------------------------------------- main


def main() -> int:  # noqa: C901, PLR0915 - a linear script reads best top to bottom
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--seed", type=int, default=2026)
    ap.add_argument("--report", default=str(REPO / "ml" / "reports" / "phase2_gate.md"))
    args = ap.parse_args()
    started = time.time()
    tmp = Path(tempfile.mkdtemp(prefix="phase2_gate_"))

    print("1/6 fresh database + local API ...", flush=True)
    reset_database()
    proc = start_api(tmp / "api.log")
    try:
        return run(args, started, tmp)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=20)
        except subprocess.TimeoutExpired:
            proc.kill()


def run(args, started: float, tmp: Path) -> int:  # noqa: C901, PLR0912, PLR0915
    # ---- accounts, cars, devices through the real API
    print("2/6 owners, cars, devices (via the API) ...", flush=True)
    admin_email = "gate-admin@example.com"
    api("POST", "/auth/register", {"email": admin_email, "password": PASSWORD,
                                   "display_name": "admin", "consents": ["service_processing"]})
    psql(f"UPDATE users SET is_admin = true WHERE email = '{admin_email}'")
    admin = api("POST", "/auth/login", {"email": admin_email, "password": PASSWORD})["access_token"]

    cars = []
    for k, pid in enumerate(profile_ids()):
        p = load_profile(pid)
        email = f"owner{k}@example.com"
        consents = ["service_processing"] + (["location"] if k in CONSENTING else [])
        api("POST", "/auth/register", {"email": email, "password": PASSWORD,
                                       "display_name": f"owner{k}", "consents": consents})
        tok = api("POST", "/auth/login", {"email": email, "password": PASSWORD})["access_token"]
        v = api("POST", "/vehicles", {"make": p.make, "model": p.model, "year": p.years[0],
                                      "engine": p.engine, "cylinders": p.cylinders}, tok)
        dev = api("POST", "/admin/devices", {}, admin)
        api("POST", f"/vehicles/{v['id']}/device",
            {"device_id": dev["id"], "claim_code": dev["claim_code"]}, tok)
        _, secret = dev["token"].split(".", 1)
        cars.append({"k": k, "pid": pid, "vehicle_id": v["id"], "device_id": dev["id"],
                     "secret": secret, "location": k in CONSENTING})  # fmt: skip

    # ---- simulate + stream over HTTP
    print(f"3/6 simulating {args.days} days x {len(cars)} cars and uploading ...", flush=True)
    http_s = n_requests = 0
    for car in cars:
        seed = args.seed + car["k"]
        tl = simulate(car["pid"], days=args.days, seed=seed, faults=FAULTS.get(car["pid"]))
        start, end = int(tl.trips[0].t_ms[0]), int(tl.trips[-1].t_ms[-1])
        net = Network.lagos(start, end + 3 * DAY_MS, seed=seed)
        sink = RecordingSink(BASE, car["device_id"], car["secret"], sim_clock=True)
        t = time.time()
        run_ = Device(car["device_id"], tl, seed=seed, network=net, sink=sink).run()
        car.update(tl=tl, run=run_, sink=sink, net=net, wall_s=time.time() - t)
        http_s += sink.http_s
        n_requests += len(sink.sent)
        print(f"    {car['pid']:26s} {len(tl.trips):3d} trips  {len(run_.produced):4d} batches"
              f"  {len(sink.sent):4d} requests  {car['wall_s']:.0f} s", flush=True)  # fmt: skip

    print("4/6 waiting for trip assembly to finish ...", flush=True)
    for _ in range(180):
        busy = scalar(
            "SELECT count(*) FROM jobs WHERE status IN ('queued','running','failed') "
            "AND run_at < now() + interval '1 minute'"
        )
        if busy == 0:
            break
        time.sleep(2)

    snapshot_sql = {
        "readings": "SELECT count(*) FROM readings",
        "batches": "SELECT count(*) FROM device_batches",
        "positions": "SELECT count(*) FROM positions",
        "dtc_events": "SELECT count(*) FROM dtc_events",
        "mode06": "SELECT count(*) FROM mode06_results",
    }
    before = {k: scalar(q) for k, q in snapshot_sql.items()}

    # ---- replay everything, shuffled
    print("5/6 replaying every batch again, shuffled ...", flush=True)
    replay = [(car, body) for car in cars for kind, body, _, _ in car["sink"].sent
              if kind == "batch"]  # fmt: skip
    random.Random(args.seed).shuffle(replay)
    last_ms = max(int(c["tl"].trips[-1].t_ms[-1]) for c in cars) + 4 * DAY_MS
    replay_dups = replay_new = 0
    t = time.time()
    sinks = {c["device_id"]: FreematicsHttpSink(BASE, c["device_id"], c["secret"],
                                                sim_clock=True) for c in cars}  # fmt: skip
    for car, body in replay:
        ack = sinks[car["device_id"]].post("batch", body, last_ms)
        replay_dups += bool(ack["dup"])
        replay_new += not ack["dup"]
    replay_s = time.time() - t
    after = {k: scalar(q) for k, q in snapshot_sql.items()}

    # ---- verify
    print("6/6 checking ...", flush=True)
    dup_rows = scalar(
        "SELECT count(*) - count(DISTINCT (vehicle_id, trip_id, ts, signal_id, value)) "
        "FROM readings"
    )
    rows, checks = [], []
    tot_prod = tot_stored = 0
    for car in cars:
        run_, tl, vid = car["run"], car["tl"], car["vehicle_id"]
        prod = expected_readings(run_.produced, run_.criticals)
        stored = scalar(f"SELECT count(*) FROM readings WHERE vehicle_id = '{vid}'")
        fixes = sum(len(b["g"]) for b in run_.produced)
        pos = scalar(f"SELECT count(*) FROM positions WHERE vehicle_id = '{vid}'")
        trips_db = scalar(f"SELECT count(*) FROM trips WHERE vehicle_id = '{vid}'")
        statuses = dict(
            line.split("|")
            for line in psql(
                f"SELECT status, count(*) FROM trips WHERE vehicle_id = '{vid}' GROUP BY 1"
            ).splitlines()
        )
        batches_db = scalar(
            f"SELECT count(*) FROM device_batches WHERE device_id = '{car['device_id']}'"
        )
        dups_sent = sum(d for k, _, _, d in car["sink"].sent if k == "batch")
        late = sum(
            1 for a, b in zip(car["sink"].sent, car["sink"].sent[1:], strict=False)
            if a[0] == b[0] == "batch" and not b[3]
            and json.loads(gzip.decompress(b[1]))["seq"] < json.loads(gzip.decompress(a[1]))["seq"]
            and json.loads(gzip.decompress(b[1]))["trip"] == json.loads(gzip.decompress(a[1]))["trip"]
        )  # fmt: skip
        dtcs = set(psql(f"SELECT DISTINCT code FROM dtc_events WHERE vehicle_id = '{vid}'").split())
        gt_codes = {
            c for row in tl.ground_truth()["faults"] for c, w in row["codes"].items()
            if w["pending"]
        }  # fmt: skip
        expect_pos = fixes if car["location"] else 0
        tot_prod += prod
        tot_stored += stored
        rows.append({
            "car": car["pid"], "trips_sim": len(tl.trips), "trips_db": trips_db,
            "statuses": statuses, "batches": len(run_.produced), "batches_db": batches_db,
            "readings_prod": prod, "readings_db": stored, "fixes": fixes, "positions_db": pos,
            "location": car["location"], "dup_sent": dups_sent, "late": late,
            "outage_pct": round(car["net"].outage_share(int(tl.trips[0].t_ms[0]),
                                                        int(tl.trips[-1].t_ms[-1])) * 100, 1),
            "budget": run_.budget(), "dtcs": sorted(dtcs), "gt_codes": sorted(gt_codes),
            "faults": [f.name for f in tl.faults], "reboots": run_.reboots,
            "statuses_http": dict(car["sink"].statuses),
            "req_per_s": len(car["sink"].sent) / max(car["sink"].http_s, 1e-9),
            "rows_per_s": stored / max(car["sink"].http_s, 1e-9),
        })  # fmt: skip
        checks += [
            (f"{car['pid']}: readings stored = produced", stored == prod, f"{stored} / {prod}"),
            (f"{car['pid']}: batches stored = produced", batches_db == len(run_.produced),
             f"{batches_db} / {len(run_.produced)}"),
            (f"{car['pid']}: positions = {'all fixes' if car['location'] else '0 (no consent)'}",
             pos == expect_pos, f"{pos} / {expect_pos}"),
            (f"{car['pid']}: trips assembled = simulated", trips_db == len(tl.trips)
             and statuses.get("open", "0") == "0", f"{trips_db} / {len(tl.trips)}"),
            (f"{car['pid']}: nothing dropped from flash", not run_.dropped,
             f"{len(run_.dropped)} dropped"),
        ]  # fmt: skip
        if gt_codes:
            checks.append((f"{car['pid']}: fault DTCs reached the database", gt_codes <= dtcs,
                           f"db {sorted(dtcs)} vs simulated {sorted(gt_codes)}"))  # fmt: skip
    dead_jobs = scalar("SELECT count(*) FROM jobs WHERE status = 'dead'")
    checks += [
        ("duplicate reading rows = 0", dup_rows == 0, str(dup_rows)),
        ("replay: every batch acknowledged as duplicate", replay_new == 0,
         f"{replay_dups} dup, {replay_new} new"),
        ("replay: nothing changed in the database", before == after,
         ", ".join(f"{k} {before[k]}->{after[k]}" for k in before)),
        ("no dead jobs", dead_jobs == 0, str(dead_jobs)),
    ]  # fmt: skip
    passed = all(ok for _, ok, _ in checks)

    write_report(Path(args.report), args, rows, checks, passed, tot_prod, tot_stored, dup_rows,
                 n_requests, http_s, len(replay), replay_s, time.time() - started)  # fmt: skip
    for name, ok, detail in checks:
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}: {detail}")
    print(f"\n{'GATE PASSED' if passed else 'GATE FAILED'}  ->  {args.report}")
    return 0 if passed else 1


def write_report(path, args, rows, checks, passed, tot_prod, tot_stored, dup_rows, n_requests,
                 http_s, n_replay, replay_s, wall_s) -> None:  # fmt: skip
    L = []
    w = L.append
    w("# Phase 2 exit gate: simulated week, ingested over HTTP\n")
    w(f"Generated by `scripts/phase2_gate.py` on {datetime.now():%Y-%m-%d %H:%M} "
      f"(seed {args.seed}, {args.days} days x {len(rows)} cars). Local Docker stack only.\n")  # fmt: skip
    w(f"**Result: {'PASSED' if passed else 'FAILED'}**: {sum(ok for _, ok, _ in checks)} of "
      f"{len(checks)} checks.\n")  # fmt: skip
    w("## Headline\n")
    w("| | |\n|---|---|")
    w(f"| Readings produced by the devices | {tot_prod:,} |")
    w(f"| Readings stored | {tot_stored:,} |")
    w(f"| Duplicate reading rows | {dup_rows} |")
    w(f"| Upload requests (incl. retries and duplicates) | {n_requests:,} |")
    rates = sorted(r["req_per_s"] for r in rows)
    rows_s = sorted(r["rows_per_s"] for r in rows)
    w(f"| Ingest throughput (median of cars; time inside HTTP calls, one client) | "
      f"{rates[len(rates) // 2]:.1f} requests/s, {rows_s[len(rows_s) // 2]:,.0f} readings/s "
      f"(per car: {', '.join(f'{x:.1f}' for x in rates)} req/s) |")
    w(f"| Replay | {n_replay:,} batches re-sent shuffled in {replay_s:.0f} s |")
    w(f"| Total run time | {wall_s / 60:.1f} min |\n")
    w("## Checks\n")
    w("| Check | Result | Detail |\n|---|---|---|")
    for name, ok, detail in checks:
        w(f"| {name} | {'✅' if ok else '❌'} | {detail} |")
    w("\n## Per car\n")
    w("| Car | Trips sim / DB | Trip status | Batches | Readings produced / stored | "
      "Positions | Dups sent | Late batches | Outage % | Reboots |")
    w("|---|---|---|---|---|---|---|---|---|---|")
    for r in rows:
        st = ", ".join(f"{k} {v}" for k, v in sorted(r["statuses"].items()))
        pos = f"{r['positions_db']:,} ({'consent' if r['location'] else 'no consent'})"
        w(f"| {r['car']} | {r['trips_sim']} / {r['trips_db']} | {st} | {r['batches']} | "
          f"{r['readings_prod']:,} / {r['readings_db']:,} | {pos} | {r['dup_sent']} | "
          f"{r['late']} | {r['outage_pct']} | {r['reboots']} |")
    w("\n## Faults and DTCs\n")
    w("| Car | Fault (fast curve) | DTCs the simulated ECU set | DTCs in the database |")
    w("|---|---|---|---|")
    for r in rows:
        if r["faults"]:
            w(f"| {r['car']} | {', '.join(r['faults'])} | {', '.join(r['gt_codes']) or 'none yet'}"
              f" | {', '.join(r['dtcs']) or 'none'} |")
    w("\n## Data budget (wire bytes incl. HTTPS overhead estimate)\n")
    w("| Car | Driving h | KB per driving hour | MB/month at 2 h/day |\n|---|---|---|---|")
    for r in rows:
        b = r["budget"]
        w(f"| {r['car']} | {b['driving_h']} | {b['wire_kb_per_h']} | {b['mb_per_month_2h_day']} |")
    w("\n## Notes\n")
    w("- Late batches = batches that reached the server after a later batch of the same trip "
      "(freshest-first after outages, ADR-010). `late_data` trips were assembled, then "
      "re-assembled when an older batch arrived.")
    w("- The local API ran with `INGEST_SIM_CLOCK=true` (the server clock follows the "
      "simulation; refused in production) and a raised rate limit (429s are unit-tested).")
    w("- Faults use faster curves than the library defaults so codes appear inside one week; "
      "the ground truth is from `Timeline.ground_truth()`.")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(L) + "\n", encoding="utf-8", newline="\n")


if __name__ == "__main__":
    sys.exit(main())

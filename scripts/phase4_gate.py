"""Phase 4 exit gate (docs/plans/phase4-plan.md step 6): fault codes explained end to end.

    docker compose up -d db
    cd simulator
    uv run python ../scripts/phase4_gate.py              # real model: well under $0.01
    uv run python ../scripts/phase4_gate.py --no-llm     # templates only, free

Local only (nothing touches Neon or Railway). Reuses the Phase 2 gate's plumbing: a
throwaway `autocare_gate4` database and its own API on port 8014 with the simulation clock.

1. A Toyota Corolla develops a **vacuum leak** and a **misfire on cylinder 2** over a week
   of Lagos driving, uploaded over HTTP with Lagos coverage.
2. The pipeline assembles and summarises each trip, then `dtc.interpret` explains the
   active codes (LLM within budget; else reviewed generic / template).
3. `GET /vehicles/{id}/codes` is checked:
   - every code the simulator set (P0171, P0302, ...) is listed and explained
   - each explanation has the full shape, and its severity equals the definition's
   - it mentions no code outside the car's active codes (grounding)
   - pending codes carry the pending message
   - with the LLM: answers are personalised, no call failed, and the cache held (far
     fewer calls than trips), costing well under $0.05
Writes `ml/reports/phase4_gate.md`. Exit code 1 if the gate fails.
"""

import os
import re
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
from autocare_sim.faults import fault  # noqa: E402
from autocare_sim.network import Network  # noqa: E402
from autocare_sim.run import LAGOS, simulate  # noqa: E402

g.GATE_DB, g.PORT = "autocare_gate4", 8014
g.BASE = f"http://127.0.0.1:{g.PORT}"
NO_LLM = "--no-llm" in sys.argv
DAYS = int(sys.argv[sys.argv.index("--days") + 1]) if "--days" in sys.argv else 7
PID, SEED = "toyota_corolla_1_8_2009", 41
FAULTS = [fault("vacuum_leak", onset=1, ramp=4), fault("misfire", onset=1, ramp=3, cylinder=2)]
DEFS = {d["code"]: d for d in yaml.safe_load(
    (REPO / "backend/app/config/dtc/definitions.yaml").read_text("utf-8"))["codes"]}  # fmt: skip
CODE_RE = re.compile(r"\b[PCBU][0-9A-F]{4}\b")
KEYS = {"source", "summary", "severity", "likely_causes", "what_to_do_now",
        "questions_for_mechanic", "confidence"}  # fmt: skip
PENDING = "hasn't confirmed it"  # in the template's pending line; the prompt asks the same
DAY_MS = 86_400_000


def psql(sql: str) -> str:
    return g.psql(sql, db=g.GATE_DB)


def scalar(sql: str) -> int:
    return int(psql(sql) or 0)


def register(email: str) -> str:
    g.api("POST", "/auth/register", {"email": email, "password": g.PASSWORD,
          "display_name": email.split("@")[0], "consents": ["service_processing"]})  # fmt: skip
    return g.api("POST", "/auth/login", {"email": email, "password": g.PASSWORD})["access_token"]


def main() -> int:
    started = time.time()
    tmp = Path(__import__("tempfile").mkdtemp(prefix="phase4_gate_"))
    if NO_LLM:
        os.environ["LLM_API_KEY"] = ""  # the API process inherits this: templates only
    print(f"1/5 fresh database + local API (LLM {'off' if NO_LLM else 'on'}) ...", flush=True)
    g.reset_database()
    proc = g.start_api(tmp / "api.log")
    try:
        return run(started)
    finally:
        g.stop_api(proc)


def explanation_problems(c: dict, active: set[str]) -> list[str]:
    x, d = c["explanation"], DEFS.get(c["code"])
    p = []
    if x is None:
        return ["no explanation"]
    if missing := KEYS - set(x):
        p.append(f"missing {sorted(missing)}")
    if not 1 <= len(x.get("likely_causes", [])) <= 5:
        p.append("likely_causes not 1-5")
    if not 1 <= len(x.get("questions_for_mechanic", [])) <= 4:
        p.append("questions not 1-4")
    if d and not (c["severity"] == x.get("severity") == d["severity"]):
        p.append(f"severity {c['severity']}/{x.get('severity')} vs definition {d['severity']}")
    causes = [f"{k['cause']} {k['why']}" for k in x.get("likely_causes", [])]
    text = " ".join([x.get("summary", ""), x.get("what_to_do_now", ""),
                     *x.get("questions_for_mechanic", []), *causes])  # fmt: skip
    if invented := set(CODE_RE.findall(text)) - active:
        p.append(f"mentions {sorted(invented)} (not active)")
    if c["status"] == "pending" and x["source"] == "template" and PENDING not in x["summary"]:
        p.append("pending without the pending message")
    if c["status"] == "pending" and x["source"] != "template" and "light" not in text.lower():
        p.append("pending answer doesn't mention the warning light")
    return p


def run(started: float) -> int:  # noqa: C901, PLR0915 - a linear script reads best top to bottom
    now = datetime.now(UTC)
    today = now.astimezone(LAGOS).date()
    start = datetime.combine(today - timedelta(days=DAYS), datetime.min.time(), LAGOS)

    print("2/5 owner, car, device (via the API) ...", flush=True)
    admin_email = "gate4-admin@example.com"
    register(admin_email)
    psql(f"UPDATE users SET is_admin = true WHERE email = '{admin_email}'")
    admin = g.api("POST", "/auth/login", {"email": admin_email, "password": g.PASSWORD})
    admin = admin["access_token"]
    owner_email = "gate4-owner@example.com"
    tok = register(owner_email)
    v = g.api("POST", "/vehicles", {"make": "Toyota", "model": "Corolla", "year": 2009,
              "engine": "1ZZ-FE 1.8L I4",
              "odometer_at_install_km": 150_000, "installed_at": start.date().isoformat()},
              tok)  # fmt: skip
    # A VIN the model must never see (set directly: this throwaway one has no check digit).
    psql(f"UPDATE vehicles SET vin = 'GATE4VIN000000001' WHERE id = '{v['id']}'")
    dev = g.api("POST", "/admin/devices", {}, admin)
    g.api("POST", f"/vehicles/{v['id']}/device",
          {"device_id": dev["id"], "claim_code": dev["claim_code"]}, tok)  # fmt: skip

    print(f"3/5 simulating {DAYS} days with a vacuum leak + misfire, uploading ...", flush=True)
    tl = simulate(PID, days=DAYS, seed=SEED, start=start, faults=FAULTS)
    tl.trips = [t for t in tl.trips if t.end < now - timedelta(minutes=10)]
    t0, t1 = int(tl.trips[0].t_ms[0]), int(tl.trips[-1].t_ms[-1])
    sink = g.RecordingSink(g.BASE, dev["id"], dev["token"].split(".", 1)[1], sim_clock=True)
    net = Network.lagos(t0, t1 + 3 * DAY_MS, seed=SEED)
    run_ = Device(dev["id"], tl, seed=SEED, network=net, sink=sink).run()
    gt = tl.ground_truth()
    sim_codes = {c for row in gt["faults"] for c, w in row["codes"].items() if w["pending"]}
    print(f"    {len(tl.trips)} trips, {len(run_.produced)} batches, simulator set "
          f"{sorted(sim_codes) or 'no codes'}", flush=True)  # fmt: skip

    print("4/5 waiting for assembly, summaries and interpretation ...", flush=True)
    for _ in range(900):
        busy = scalar("SELECT count(*) FROM jobs WHERE status IN ('queued','running','failed') "
                      "AND run_at < now() + interval '1 minute'")  # fmt: skip
        if busy == 0:
            break
        time.sleep(2)

    print("5/5 checking the codes API ...", flush=True)
    tok = g.api("POST", "/auth/login", {"email": owner_email, "password": g.PASSWORD})
    tok = tok["access_token"]
    body = g.api("GET", f"/vehicles/{v['id']}/codes", None, tok)
    active_list = [c for s in ("stored", "pending", "permanent") for c in body[s]]
    active = {c["code"] for c in active_list}
    usage = g.api("GET", "/admin/llm/usage", None, admin)
    dead = scalar("SELECT count(*) FROM jobs WHERE status = 'dead'")
    interpret_jobs = scalar("SELECT count(*) FROM jobs WHERE name = 'dtc.interpret' "
                            "AND status = 'succeeded'")  # fmt: skip
    problems = {f"{c['code']} {c['status']}": explanation_problems(c, active) for c in active_list}
    sources = sorted({c["explanation"]["source"] for c in active_list if c["explanation"]})

    checks = [
        ("the simulator set the injected faults' codes", {"P0171", "P0302"} <= sim_codes,
         f"simulated {sorted(sim_codes)}"),
        ("every simulated code is listed as active", sim_codes <= active,
         f"API {sorted(active)} vs simulated {sorted(sim_codes)}"),
        ("every active code is explained, shaped, tiered and grounded",
         bool(active_list) and not any(problems.values()),
         "; ".join(f"{k}: {', '.join(p)}" for k, p in problems.items() if p) or "all clean"),
        ("no dead jobs", dead == 0, f"{dead} dead"),
        ("dtc.interpret ran for the trips", interpret_jobs > 0, f"{interpret_jobs} runs"),
    ]  # fmt: skip
    if NO_LLM:
        checks.append(("with no key: templates / reviewed generic, no LLM calls",
                       usage["calls"] == 0 and "personalised" not in sources,
                       f"{usage['calls']} calls, sources {sources}"))  # fmt: skip
    else:
        checks += [
            ("answers are personalised", sources == ["personalised"], f"sources {sources}"),
            ("no failed LLM calls (0 schema / grounding failures)", usage["failed"] == 0,
             f"{usage['ok']} ok, {usage['failed']} failed"),
            ("the cache held: fewer calls than interpretation runs x codes",
             usage["calls"] < interpret_jobs * max(1, len(active)),
             f"{usage['calls']} calls for {interpret_jobs} runs x {len(active)} codes"),
            ("cost well under $0.05", usage["spent_usd"] < 0.05, f"${usage['spent_usd']:.4f}"),
        ]  # fmt: skip
    passed = all(ok for _, ok, _ in checks)

    out = REPO / "ml" / "reports" / "phase4_gate.md"
    lines = []
    w = lines.append
    w("# Phase 4 gate: fault codes explained end to end\n")
    w(f"Run by `scripts/phase4_gate.py{' --no-llm' if NO_LLM else ''}` on "
      f"{datetime.now():%Y-%m-%d %H:%M} ({DAYS} days, seed {SEED}, LLM "
      f"{'off' if NO_LLM else usage['model']}). Local Docker stack only.\n")  # fmt: skip
    w(f"**Result: {'PASSED' if passed else 'FAILED'}**: {sum(ok for _, ok, _ in checks)} of "
      f"{len(checks)} checks.\n")  # fmt: skip
    w("## The car\n")
    w(f"Toyota Corolla 1.8 with a vacuum leak (onset day 1) and a cylinder-2 misfire (day 1). "
      f"{len(tl.trips)} trips, {len(run_.produced)} batches over HTTP with Lagos coverage.\n")
    w("| Code | Status | Severity | Stop safely | Source | First seen |")
    w("|---|---|---|---|---|---|")
    for c in active_list:
        w(f"| {c['code']} | {c['status']} | {c['severity']} | {c['stop_safely']} | "
          f"{c['explanation']['source']} | {c['first_seen'][:16]} |")
    w(f"\nLLM: {usage['calls']} calls ({usage['ok']} ok, {usage['failed']} failed), "
      f"{usage['tokens_in']:,} tokens in / {usage['tokens_out']:,} out, "
      f"**${usage['spent_usd']:.4f}**, for {interpret_jobs} interpretation runs.\n")
    w("## Checks\n")
    w("| Check | Result | Detail |")
    w("|---|---|---|")
    for name, ok, detail in checks:
        w(f"| {name} | {'PASS' if ok else 'FAIL'} | {detail} |")
    w("\n## What the owner sees (first code)\n")
    if active_list:
        c = active_list[0]
        x = c["explanation"]
        w(f"**{c['code']} ({c['status']})**: {c['description']}\n")
        w(f"{x['summary']}\n")
        w("Likely causes:")
        w("\n".join(f"{i}. **{k['cause']}**: {k['why']}"
                    for i, k in enumerate(x["likely_causes"], 1)))  # fmt: skip
        w(f"\nWhat to do now: {x['what_to_do_now']}\n")
    w("## Notes\n")
    w("- The car has a VIN and an owner email; neither can reach the model (the context has "
      "no field for them, tested in `tests/test_dtc_context.py`).")
    w(f"- Took {time.time() - started:.0f} s.")
    out.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")

    print()
    for name, ok, detail in checks:
        print(f"{'PASS' if ok else 'FAIL'}  {name}: {detail}")
    print(f"\n{'PASSED' if passed else 'FAILED'}; report: {out.relative_to(REPO)}")
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())

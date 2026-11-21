"""Phase 3 Restfox request list: maintenance forecast, service records, odometer, trips.

    uv run python restfox/phase3_requests.py export                       # Postman v2.1 file
    uv run python restfox/phase3_requests.py run http://localhost:8000    # replay + check

No admin needed. Each run creates an owner, a car and a stranger, and deletes them at the
end. In Restfox, set `base` and `run` (any new word per run, so emails don't collide).
"""

import json
import re
import sys
import uuid
from pathlib import Path

import httpx

PW = "restfox-pass-123"
J = "application/json"


def R(n, name, method, path, *, body=None, auth=None, expect, save=None, check=None, note=""):
    return dict(n=n, name=name, method=method, path=path, body=body, auth=auth, expect=expect,
                save=save or {}, check=check or {}, note=note)  # fmt: skip


def user(var):
    return {"email": f"{{{{{var}}}}}", "password": PW, "display_name": var,
            "consents": ["service_processing"]}  # fmt: skip


VEHICLE = {"make": "Toyota", "model": "Corolla", "year": 2010, "engine": "2ZR-FE 1.8L I4",
           "odometer_at_install_km": 120000, "installed_at": "2026-09-01",
           "oil_type": "semi_synthetic"}  # fmt: skip
V = "/vehicles/{{vid}}"

REQUESTS = [
    R(1, "Liveness", "GET", "/health", expect=200),
    R(2, "Readiness (DB + migrations)", "GET", "/health/ready", expect=200,
      check={"checks.migrations": "head"}),
    R(3, "Register owner", "POST", "/auth/register", body=user("email_owner"), expect=201),
    R(4, "Log in owner", "POST", "/auth/login",
      body={"email": "{{email_owner}}", "password": PW}, expect=200,
      save={"token": "access_token"}),
    R(5, "Add a car (install odometer 120,000 km, semi-synthetic oil)", "POST", "/vehicles",
      auth="token", body=VEHICLE, expect=201, save={"vid": "id"},
      check={"oil_type": "semi_synthetic"}),
    R(6, "Maintenance view: no history yet", "GET", f"{V}/maintenance", auth="token", expect=200,
      check={"odometer.estimate_km": 120000, "odometer.anchor_source": "install",
             "usage.method": "prior", "items.0.status": "unknown"}),
    R(7, "Record an oil change (with odometer)", "POST", f"{V}/service-records", auth="token",
      body={"item_key": "engine_oil", "done_on": "2026-10-01", "odometer_km": 120300,
            "notes": "Semi-synthetic 5W-30"}, expect=201, save={"rid": "id"},
      check={"odometer_check.flagged": False}),
    R(8, "Record a battery check (no odometer)", "POST", f"{V}/service-records", auth="token",
      body={"item_key": "battery_check", "done_on": "2026-06-01"}, expect=201),
    R(9, "Record an unknown item", "POST", f"{V}/service-records", auth="token",
      body={"item_key": "flux_capacitor", "done_on": "2026-10-01"}, expect=422),
    R(10, "Record a service in the future", "POST", f"{V}/service-records", auth="token",
      body={"item_key": "engine_oil", "done_on": "2999-01-01"}, expect=422),
    R(11, "List service records", "GET", f"{V}/service-records", auth="token", expect=200),
    R(12, "Maintenance view: oil and battery now forecast", "GET", f"{V}/maintenance",
      auth="token", expect=200, check={"odometer.estimate_km": 120300},
      note="Battery checked 2026-06-01 + 6 months; oil 2026-10-01 + 3 months or 5,000 km"),
    R(13, "Odometer from the dash, agreeing", "POST", f"{V}/odometer", auth="token",
      body={"km": 120350}, expect=201, check={"mismatch": False}),
    R(14, "Odometer far off (flagged, not trusted)", "POST", f"{V}/odometer", auth="token",
      body={"km": 90000}, expect=201, check={"mismatch": True}),
    R(15, "Maintenance view keeps the trusted estimate", "GET", f"{V}/maintenance", auth="token",
      expect=200, check={"odometer.estimate_km": 120350, "odometer.last_entry_mismatch": True}),
    R(16, "Change oil type to full synthetic", "PATCH", V, auth="token",
      body={"oil_type": "full_synthetic"}, expect=200, check={"oil_type": "full_synthetic"}),
    R(17, "Trips (none yet without a device)", "GET", f"{V}/trips", auth="token", expect=200),
    R(18, "Trips with a bad page size", "GET", f"{V}/trips?limit=101", auth="token", expect=422),
    R(19, "Delete the oil record (its odometer value goes too)", "DELETE",
      f"{V}/service-records/{{{{rid}}}}", auth="token", expect=204),
    R(20, "Delete it again", "DELETE", f"{V}/service-records/{{{{rid}}}}", auth="token",
      expect=404),
    R(21, "Register a stranger", "POST", "/auth/register", body=user("email_stranger"),
      expect=201),
    R(22, "Log in stranger", "POST", "/auth/login",
      body={"email": "{{email_stranger}}", "password": PW}, expect=200,
      save={"token_s": "access_token"}),
    R(23, "Stranger reads the maintenance view (hidden)", "GET", f"{V}/maintenance",
      auth="token_s", expect=404),
    R(24, "Stranger records a service (hidden)", "POST", f"{V}/service-records", auth="token_s",
      body={"item_key": "engine_oil", "done_on": "2026-10-01"}, expect=404),
    R(25, "Delete the car", "DELETE", V, auth="token", expect=204),
    R(26, "Delete the owner account", "DELETE", "/me", auth="token", expect=204),
    R(27, "Delete the stranger account", "DELETE", "/me", auth="token_s", expect=204),
]  # fmt: skip


def _get(doc, dotted: str):
    for part in dotted.split("."):
        doc = doc[int(part)] if isinstance(doc, list) else doc[part]
    return doc


def export(path: Path) -> None:
    items = []
    for r in REQUESTS:
        headers = [{"key": "Content-Type", "value": J}] if r["body"] is not None else []
        if r["auth"]:
            headers.append({"key": "Authorization", "value": f"Bearer {{{{{r['auth']}}}}}"})
        desc = f"Expect HTTP {r['expect']}."
        if r["check"]:
            desc += " Check: " + ", ".join(f"{k} = {json.dumps(v)}" for k, v in r["check"].items())
            desc += "."
        if r["save"]:
            desc += " Then save: " + ", ".join(f"{v} = response.{f}" for v, f in r["save"].items())
            desc += "."
        if r["note"]:
            desc += " " + r["note"] + "."
        req = {"method": r["method"], "header": headers, "url": "{{base}}" + r["path"],
               "description": desc}  # fmt: skip
        if r["body"] is not None:
            req["body"] = {"mode": "raw", "raw": json.dumps(r["body"], indent=2),
                           "options": {"raw": {"language": "json"}}}  # fmt: skip
        items.append({"name": f"{r['n']:02d} {r['name']} -> {r['expect']}", "request": req})
    variables = {"base": "http://localhost:8000", "run": "r1",
                 "email_owner": "p3-owner-{{run}}@example.com",
                 "email_stranger": "p3-stranger-{{run}}@example.com"}  # fmt: skip
    coll = {
        "info": {
            "name": "AutoCare Phase 3 (maintenance)",
            "_postman_id": str(uuid.uuid5(uuid.NAMESPACE_URL, "autocare-p3")),
            "description": "Run in order. Change `run` before each full run.",
            "schema": "https://schema.getpostman.com/json/collection/v2.1.0/collection.json",
        },
        "item": items,
        "variable": [{"key": k, "value": v} for k, v in variables.items()],
    }
    path.write_text(json.dumps(coll, indent=2) + "\n", encoding="utf-8", newline="\n")
    print(f"wrote {path} ({len(items)} requests)")


def run(base: str) -> int:
    tag = uuid.uuid4().hex[:6]
    env = {"email_owner": f"p3-owner-{tag}@example.com",
           "email_stranger": f"p3-stranger-{tag}@example.com"}  # fmt: skip

    def sub(x):
        if isinstance(x, str):
            return re.sub(r"\{\{(\w+)\}\}", lambda m: str(env[m.group(1)]), x)
        if isinstance(x, dict):
            return {k: sub(v) for k, v in x.items()}
        return x

    base = base.rstrip("/")
    print(f"Target: {base}\n")
    failures = skipped = 0
    with httpx.Client(base_url=base, timeout=30) as c:
        for r in REQUESTS:
            needed = set(re.findall(r"\{\{(\w+)\}\}", json.dumps([r["path"], r["body"]])))
            if r["auth"]:
                needed.add(r["auth"])
            missing = sorted(needed - env.keys())
            if missing:
                skipped += 1
                print(f"SKIP {r['n']:02d} {r['method']:6} {r['name']}: needs {missing}")
                continue
            headers = {"Authorization": f"Bearer {env[r['auth']]}"} if r["auth"] else {}
            resp = c.request(r["method"], sub(r["path"]), json=sub(r["body"]), headers=headers)
            ok = resp.status_code == r["expect"]
            bad = []
            if ok and r["check"]:
                got = resp.json()
                bad = [f"{k}={_get(got, k)!r} (want {v!r})"
                       for k, v in r["check"].items() if _get(got, k) != v]  # fmt: skip
                ok = not bad
            failures += not ok
            print(f"{'PASS' if ok else 'FAIL'} {r['n']:02d} {r['method']:6} {r['name']}: "
                  f"{resp.status_code} (want {r['expect']})"
                  + (f"  {'; '.join(bad)}" if bad else ""))  # fmt: skip
            if not ok and not bad:
                print(f"     body: {resp.text[:200]!r}")
                if r["n"] == 1:
                    print("\n/health failed: wrong URL or the app is down. Stopping.")
                    return 1
            if resp.status_code == r["expect"]:
                for var, field in r["save"].items():
                    env[var] = str(resp.json()[field])
    passed = len(REQUESTS) - failures - skipped
    print(f"\n{passed}/{len(REQUESTS)} passed, {failures} failed, {skipped} skipped")
    return 1 if failures or skipped else 0


if __name__ == "__main__":
    here = Path(__file__).parent
    if sys.argv[1:2] == ["export"]:
        export(here / "autocare_phase3.postman_collection.json")
    elif sys.argv[1:2] == ["run"]:
        sys.exit(run(sys.argv[2] if len(sys.argv) > 2 else "http://localhost:8000"))
    else:
        print(__doc__)

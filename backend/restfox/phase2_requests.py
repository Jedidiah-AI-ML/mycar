"""Phase 2 Restfox request list: device provisioning, claim, status and ingestion.

    uv run python restfox/phase2_requests.py export                       # Postman v2.1 file
    uv run python restfox/phase2_requests.py run http://localhost:8000    # replay + check

Needs an **admin account** (provisioning is admin-only, and `is_admin` is never set by
the API). Create one once: register through the API, then in the database run
    UPDATE users SET is_admin = true WHERE email = 'you@example.com';
For `run`, put its login in environment variables, so it's never on the command line:
    $env:AUTOCARE_ADMIN_EMAIL = "you@example.com"; $env:AUTOCARE_ADMIN_PASSWORD = "..."
In Restfox, set the variables `base`, `run`, `admin_email` and `admin_password`.

Ingest requests send plain JSON (the gateway accepts it as well as gzip), so they work
from Restfox. Each run creates one owner, one car and one device; the last requests
delete the car and the account (device rows stay: there is no device delete).
"""

import json
import os
import re
import sys
import uuid
from pathlib import Path

import httpx

PW = "restfox-pass-123"
J = "application/json"
T0 = 1_763_290_000_000  # fixed device time: the gateway's skew correction shifts it to now


def R(n, name, method, path, *, body=None, auth=None, expect, save=None, check=None, note=""):
    return dict(n=n, name=name, method=method, path=path, body=body, auth=auth, expect=expect,
                save=save or {}, check=check or {}, note=note)  # fmt: skip


def batch(seq: int, events: list | None = None, final: bool = False) -> dict:
    s = {
        "rpm": [[i * 1000, 820 + 10 * i] for i in range(10)],
        "speed": [[i * 1000, 3 * i] for i in range(10)],
        "coolant": [[0, 88], [7500, 89]],
        "batt_v": [[0, 14.05], [5000, 14.1]],
    }
    return {"v": 1, "device_id": "{{device_id}}", "fw": "0.1.0", "boot": 0, "trip": 1,
            "seq": seq, "final": final, "t0": T0 + seq * 10_000, "sent_at": T0 + 120_000,
            "s": s, "g": [[0, 6.5244, 3.3792, 0.0]], "ev": events or [],
            "net": {"rssi": -77}}  # fmt: skip


OWNER = {"email": "{{email_owner}}", "password": PW, "display_name": "owner",
         "consents": ["service_processing"]}  # fmt: skip
ING = "/ingest/freematics_one_plus"

REQUESTS = [
    R(1, "Liveness", "GET", "/health", expect=200),
    R(2, "Readiness (DB + migrations)", "GET", "/health/ready", expect=200,
      check={"checks.migrations": "head"}),
    R(3, "Register owner", "POST", "/auth/register", body=OWNER, expect=201),
    R(4, "Log in owner", "POST", "/auth/login", body={"email": "{{email_owner}}", "password": PW},
      expect=200, save={"token": "access_token"}),
    R(5, "Log in admin", "POST", "/auth/login",
      body={"email": "{{admin_email}}", "password": "{{admin_password}}"}, expect=200,
      save={"admin_token": "access_token"}, note="Needs an account with is_admin = true"),
    R(6, "Owner adds a car", "POST", "/vehicles", auth="token",
      body={"make": "Toyota", "model": "Corolla", "year": 2009}, expect=201,
      save={"vid": "id"}),
    R(7, "Owner tries to provision (not admin)", "POST", "/admin/devices", auth="token", body={},
      expect=404),
    R(8, "Admin provisions a device", "POST", "/admin/devices", auth="admin_token", body={},
      expect=201, save={"device_id": "id", "device_token": "token", "claim_code": "claim_code"},
      note="token and claim_code are shown only now"),
    R(9, "Claim with a wrong code", "POST", "/vehicles/{{vid}}/device", auth="token",
      body={"device_id": "{{device_id}}", "claim_code": "AAAA-AAAA"}, expect=403),
    R(10, "Claim the device", "POST", "/vehicles/{{vid}}/device", auth="token",
      body={"device_id": "{{device_id}}", "claim_code": "{{claim_code}}"}, expect=201),
    R(11, "Device status (never seen)", "GET", "/vehicles/{{vid}}/device", auth="token",
      expect=200, check={"online": False}),
    R(12, "Upload without a token", "POST", f"{ING}/batches", body=batch(0), expect=401),
    R(13, "Upload with a wrong token", "POST", f"{ING}/batches", auth="bad_device_token",
      body=batch(0), expect=401),
    R(14, "Upload batch 0 (trip start)", "POST", f"{ING}/batches", auth="device_token",
      body=batch(0, [{"t": 0, "type": "trip_start"}]), expect=200, check={"dup": False}),
    R(15, "Upload batch 0 again (retry)", "POST", f"{ING}/batches", auth="device_token",
      body=batch(0, [{"t": 0, "type": "trip_start"}]), expect=200, check={"dup": True}),
    R(16, "Upload batch 2 before 1 (out of order)", "POST", f"{ING}/batches",
      auth="device_token", expect=200, check={"dup": False},
      body=batch(2, [{"t": 9000, "type": "trip_end", "reason": "ignition_off"}], final=True)),
    R(17, "Upload batch 1 (late)", "POST", f"{ING}/batches", auth="device_token",
      body=batch(1, [{"t": 0, "type": "dtc", "mode": "07", "codes": ["P0128"]}]), expect=200,
      check={"dup": False}),
    R(18, "Upload breaking the contract", "POST", f"{ING}/batches", auth="device_token",
      body={**batch(3), "v": 2}, expect=422),
    R(19, "Critical packet", "POST", f"{ING}/critical", auth="device_token", expect=200,
      body={"v": 1, "device_id": "{{device_id}}", "boot": 0, "trip": 1, "t": T0 + 25_000,
            "sent_at": T0 + 30_000, "kind": "coolant_high",
            "values": {"coolant": 118, "rpm": 800}}),
    R(20, "Device status (online now)", "GET", "/vehicles/{{vid}}/device", auth="token",
      expect=200, check={"online": True, "signal": "fair"}),
    R(21, "Admin: what was stored", "GET", "/admin/vehicles/{{vid}}/telemetry",
      auth="admin_token", expect=200,
      check={"readings": 3 * 24 + 2, "duplicate_readings": 0, "batches": 3, "positions": 0,
             "dtc_codes": ["P0128"]},
      note="3 batches x 24 samples + 2 critical values; no positions (no location consent)"),
    R(22, "Unclaim the device", "DELETE", "/vehicles/{{vid}}/device", auth="token", expect=204),
    R(23, "Upload from the unclaimed device (acked, discarded)", "POST", f"{ING}/batches",
      auth="device_token", body=batch(5), expect=200),
    R(24, "Delete the car", "DELETE", "/vehicles/{{vid}}", auth="token", expect=204),
    R(25, "Delete the owner account", "DELETE", "/me", auth="token", expect=204),
]  # fmt: skip


def _get(doc: dict, dotted: str):
    for part in dotted.split("."):
        doc = doc[part]
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
    variables = {
        "base": "http://localhost:8000", "run": "r1", "email_owner": "p2-owner-{{run}}@example.com",
        "admin_email": "", "admin_password": "", "bad_device_token": "FM1-9999." + "x" * 43,
    }  # fmt: skip
    coll = {
        "info": {
            "name": "AutoCare Phase 2 (devices + ingestion)",
            "_postman_id": str(uuid.uuid5(uuid.NAMESPACE_URL, "autocare-p2")),
            "description": "Run in order. Change `run` before each full run. Needs an admin.",
            "schema": "https://schema.getpostman.com/json/collection/v2.1.0/collection.json",
        },
        "item": items,
        "variable": [{"key": k, "value": v} for k, v in variables.items()],
    }
    path.write_text(json.dumps(coll, indent=2) + "\n", encoding="utf-8", newline="\n")
    print(f"wrote {path} ({len(items)} requests)")


def run(base: str) -> int:
    admin_email = os.environ.get("AUTOCARE_ADMIN_EMAIL")
    admin_password = os.environ.get("AUTOCARE_ADMIN_PASSWORD")
    if not admin_email or not admin_password:
        print(
            "Set AUTOCARE_ADMIN_EMAIL and AUTOCARE_ADMIN_PASSWORD first (see the top of this file)."
        )
        return 2
    tag = uuid.uuid4().hex[:6]
    env = {
        "email_owner": f"p2-owner-{tag}@example.com",
        "admin_email": admin_email,
        "admin_password": admin_password,
        "bad_device_token": "FM1-9999." + "x" * 43,
    }

    def sub(x):
        if isinstance(x, str):
            m = re.fullmatch(r"\{\{(\w+)\}\}", x)
            if m:
                return env[m.group(1)]
            return re.sub(r"\{\{(\w+)\}\}", lambda m: str(env[m.group(1)]), x)
        if isinstance(x, dict):
            return {k: sub(v) for k, v in x.items()}
        if isinstance(x, list):
            return [sub(v) for v in x]
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
            bad_checks = []
            if ok and r["check"]:
                got = resp.json()
                bad_checks = [f"{k}={_get(got, k)!r} (want {v!r})"
                              for k, v in r["check"].items() if _get(got, k) != v]  # fmt: skip
                ok = not bad_checks
            failures += not ok
            print(f"{'PASS' if ok else 'FAIL'} {r['n']:02d} {r['method']:6} {r['name']}: "
                  f"{resp.status_code} (want {r['expect']})"
                  + (f"  {'; '.join(bad_checks)}" if bad_checks else ""))  # fmt: skip
            if not ok and not bad_checks:
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
        export(here / "autocare_phase2.postman_collection.json")
    elif sys.argv[1:2] == ["run"]:
        sys.exit(run(sys.argv[2] if len(sys.argv) > 2 else "http://localhost:8000"))
    else:
        print(__doc__)

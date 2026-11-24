"""Phase 4 Restfox request list: the fault-code API's contract.

    uv run python restfox/phase4_requests.py export                       # Postman v2.1 file
    uv run python restfox/phase4_requests.py run http://localhost:8000    # replay + check

No admin needed. Codes come from a device, so this list checks the contract on a car
without one: empty lists, 404 for a code the car never reported, 422 for a bad code,
strangers and non-admins hidden. Codes with real explanations are checked end to end by
the Phase 4 gate (step 6). In Restfox, set `base` and `run` (any new word per run).
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


VEHICLE = {"make": "Toyota", "model": "Corolla", "year": 2010, "engine": "2ZR-FE 1.8L I4"}
V = "/vehicles/{{vid}}"
EMPTY = {"stop_safely": False, "stored": [], "pending": [], "permanent": [],
         "recently_cleared": []}  # fmt: skip

REQUESTS = [
    R(1, "Liveness", "GET", "/health", expect=200),
    R(2, "Readiness (DB + migrations)", "GET", "/health/ready", expect=200,
      check={"checks.migrations": "head"}),
    R(3, "Register owner", "POST", "/auth/register", body=user("email_owner"), expect=201),
    R(4, "Log in owner", "POST", "/auth/login",
      body={"email": "{{email_owner}}", "password": PW}, expect=200,
      save={"token": "access_token"}),
    R(5, "Add a car", "POST", "/vehicles", auth="token", body=VEHICLE, expect=201,
      save={"vid": "id"}),
    R(6, "Codes: none on a new car", "GET", f"{V}/codes", auth="token", expect=200,
      check=EMPTY, note="With a device, codes arrive by status with their explanations"),
    R(7, "One code the car never reported", "GET", f"{V}/codes/P0171", auth="token",
      expect=404),
    R(8, "A code in the wrong format", "GET", f"{V}/codes/p0171", auth="token", expect=422),
    R(9, "Not a code at all", "GET", f"{V}/codes/OIL", auth="token", expect=422),
    R(10, "Codes without logging in", "GET", f"{V}/codes", expect=401),
    R(11, "LLM usage as a non-admin (hidden)", "GET", "/admin/llm/usage", auth="token",
      expect=404),
    R(12, "Register a stranger", "POST", "/auth/register", body=user("email_stranger"),
      expect=201),
    R(13, "Log in stranger", "POST", "/auth/login",
      body={"email": "{{email_stranger}}", "password": PW}, expect=200,
      save={"token_s": "access_token"}),
    R(14, "Stranger lists the codes (hidden)", "GET", f"{V}/codes", auth="token_s",
      expect=404),
    R(15, "Stranger reads one code (hidden)", "GET", f"{V}/codes/P0171", auth="token_s",
      expect=404),
    R(16, "Delete the car", "DELETE", V, auth="token", expect=204),
    R(17, "Codes of a deleted car", "GET", f"{V}/codes", auth="token", expect=404),
    R(18, "Delete the owner account", "DELETE", "/me", auth="token", expect=204),
    R(19, "Delete the stranger account", "DELETE", "/me", auth="token_s", expect=204),
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
                 "email_owner": "p4-owner-{{run}}@example.com",
                 "email_stranger": "p4-stranger-{{run}}@example.com"}  # fmt: skip
    coll = {
        "info": {
            "name": "AutoCare Phase 4 (fault codes)",
            "_postman_id": str(uuid.uuid5(uuid.NAMESPACE_URL, "autocare-p4")),
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
    env = {"email_owner": f"p4-owner-{tag}@example.com",
           "email_stranger": f"p4-stranger-{tag}@example.com"}  # fmt: skip

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
        export(here / "autocare_phase4.postman_collection.json")
    elif sys.argv[1:2] == ["run"]:
        sys.exit(run(sys.argv[2] if len(sys.argv) > 2 else "http://localhost:8000"))
    else:
        print(__doc__)

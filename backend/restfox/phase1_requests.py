"""Single source of truth for the Phase 1 Restfox request list.

    uv run python restfox/phase1_requests.py export            # writes the Postman v2.1 file
    uv run python restfox/phase1_requests.py run http://localhost:8000   # replays it, checks codes

Restfox imports the exported file (Import > Postman). Set the environment variables
`base` and `run` (any new word per run, so emails don't collide). The runner here
does what you'd do by hand in Restfox: copy tokens/ids from one response into the
variables used by later requests (`save`).
"""

import json
import re
import sys
import uuid
from pathlib import Path

import httpx

PW = "restfox-pass-123"
J = "application/json"


def R(n, name, method, path, *, body=None, auth=None, expect, save=None, note=""):
    return dict(
        n=n,
        name=name,
        method=method,
        path=path,
        body=body,
        auth=auth,
        expect=expect,
        save=save or {},
        note=note,
    )


def user(email_var):
    return {
        "email": f"{{{{{email_var}}}}}",
        "password": PW,
        "display_name": email_var,
        "consents": ["service_processing"],
    }


REQUESTS = [
    R(1, "Liveness", "GET", "/health", expect=200),
    R(2, "Readiness (DB + migrations)", "GET", "/health/ready", expect=200),
    R(3, "Register owner A", "POST", "/auth/register", body=user("email_a"), expect=201),
    R(
        4,
        "Register A again, different case",
        "POST",
        "/auth/register",
        body=user("email_a") | {"email": "{{email_a_upper}}"},
        expect=409,
    ),
    R(
        5,
        "Log in A",
        "POST",
        "/auth/login",
        body={"email": "{{email_a}}", "password": PW},
        expect=200,
        save={"token": "access_token", "refresh": "refresh_token"},
    ),
    R(
        6,
        "Log in A, wrong password",
        "POST",
        "/auth/login",
        body={"email": "{{email_a}}", "password": "wrong-password-1"},
        expect=401,
    ),
    R(7, "Me (with token)", "GET", "/me", auth="token", expect=200),
    R(8, "Me (no token)", "GET", "/me", expect=401),
    R(
        9,
        "Refresh (rotates tokens)",
        "POST",
        "/auth/refresh",
        body={"refresh_token": "{{refresh}}"},
        expect=200,
        save={"token": "access_token", "old_refresh": None, "refresh": "refresh_token"},
        note="Save the old refresh token as old_refresh before running",
    ),
    R(
        10,
        "Refresh with the OLD token (reuse)",
        "POST",
        "/auth/refresh",
        body={"refresh_token": "{{old_refresh}}"},
        expect=401,
        note="Reuse revokes all A's sessions; log in again next",
    ),
    R(
        11,
        "Log in A again",
        "POST",
        "/auth/login",
        body={"email": "{{email_a}}", "password": PW},
        expect=200,
        save={"token": "access_token", "refresh": "refresh_token"},
    ),
    R(12, "My consents", "GET", "/me/consents", auth="token", expect=200),
    R(13, "Grant location consent", "POST", "/me/consents/location", auth="token", expect=201),
    R(14, "Revoke location consent", "DELETE", "/me/consents/location", auth="token", expect=204),
    R(
        15,
        "Add vehicle (Corolla 2010)",
        "POST",
        "/vehicles",
        auth="token",
        body={
            "make": "Toyota",
            "model": "Corolla",
            "year": 2010,
            "engine": "2ZR-FE 1.8L I4",
            "displacement_l": "1.8",
            "cylinders": 4,
            "odometer_at_install_km": 145000,
        },
        expect=201,
        save={"vid": "id"},
    ),
    R(
        16,
        "Add vehicle: diesel (rejected)",
        "POST",
        "/vehicles",
        auth="token",
        body={"make": "Toyota", "model": "Hilux", "year": 2012, "fuel_type": "diesel"},
        expect=422,
    ),
    R(
        17,
        "Add vehicle: VIN with letter O (rejected)",
        "POST",
        "/vehicles",
        auth="token",
        body={"make": "Toyota", "model": "Corolla", "year": 2010, "vin": "1M8GDM9AXKPO42788"},
        expect=422,
    ),
    R(18, "List my vehicles", "GET", "/vehicles", auth="token", expect=200),
    R(
        19,
        "Rename vehicle",
        "PATCH",
        "/vehicles/{{vid}}",
        auth="token",
        body={"nickname": "Blue Corolla"},
        expect=200,
    ),
    R(
        20,
        "Register B",
        "POST",
        "/auth/register",
        body=user("email_b"),
        expect=201,
        save={"user_b_id": "id"},
    ),
    R(
        21,
        "Log in B",
        "POST",
        "/auth/login",
        body={"email": "{{email_b}}", "password": PW},
        expect=200,
        save={"token_b": "access_token"},
    ),
    R(
        22,
        "A shares vehicle with B as viewer",
        "POST",
        "/vehicles/{{vid}}/access",
        auth="token",
        body={"email": "{{email_b}}", "role": "viewer"},
        expect=201,
    ),
    R(23, "B reads the vehicle", "GET", "/vehicles/{{vid}}", auth="token_b", expect=200),
    R(
        24,
        "B tries to rename it (viewer)",
        "PATCH",
        "/vehicles/{{vid}}",
        auth="token_b",
        body={"nickname": "Mine now"},
        expect=403,
    ),
    R(25, "Register C (no access)", "POST", "/auth/register", body=user("email_c"), expect=201),
    R(
        26,
        "Log in C",
        "POST",
        "/auth/login",
        body={"email": "{{email_c}}", "password": PW},
        expect=200,
        save={"token_c": "access_token"},
    ),
    R(27, "C reads A's vehicle (hidden)", "GET", "/vehicles/{{vid}}", auth="token_c", expect=404),
    R(
        28,
        "A removes B's access",
        "DELETE",
        "/vehicles/{{vid}}/access/{{user_b_id}}",
        auth="token",
        expect=204,
    ),
    R(29, "B reads the vehicle again", "GET", "/vehicles/{{vid}}", auth="token_b", expect=404),
    R(30, "Export my data", "GET", "/me/export", auth="token", expect=200),
    R(31, "Delete vehicle", "DELETE", "/vehicles/{{vid}}", auth="token", expect=204),
    R(32, "Delete my account", "DELETE", "/me", auth="token", expect=204),
    R(
        33,
        "Log in as deleted A",
        "POST",
        "/auth/login",
        body={"email": "{{email_a}}", "password": PW},
        expect=401,
    ),
]


def export(path: Path) -> None:
    items = []
    for r in REQUESTS:
        headers = [{"key": "Content-Type", "value": J}] if r["body"] is not None else []
        if r["auth"]:
            headers.append({"key": "Authorization", "value": f"Bearer {{{{{r['auth']}}}}}"})
        desc = f"Expect HTTP {r['expect']}."
        if r["save"]:
            desc += (
                " Then save: "
                + ", ".join(
                    f"{var} = response.{field}" for var, field in r["save"].items() if field
                )
                + "."
            )
        if r["note"]:
            desc += " " + r["note"] + "."
        req = {
            "method": r["method"],
            "header": headers,
            "url": "{{base}}" + r["path"],
            "description": desc,
        }
        if r["body"] is not None:
            req["body"] = {
                "mode": "raw",
                "raw": json.dumps(r["body"], indent=2),
                "options": {"raw": {"language": "json"}},
            }
        items.append({"name": f"{r['n']:02d} {r['name']} -> {r['expect']}", "request": req})
    variables = {
        "base": "http://localhost:8000",
        "run": "r1",
        "email_a": "owner-{{run}}@example.com",
        "email_a_upper": "OWNER-{{run}}@EXAMPLE.COM",
        "email_b": "viewer-{{run}}@example.com",
        "email_c": "stranger-{{run}}@example.com",
    }
    coll = {
        "info": {
            "name": "AutoCare Phase 1",
            "_postman_id": str(uuid.uuid5(uuid.NAMESPACE_URL, "autocare-p1")),
            "description": "Run in order. Change `run` before each full run.",
            "schema": "https://schema.getpostman.com/json/collection/v2.1.0/collection.json",
        },
        "item": items,
        "variable": [{"key": k, "value": v} for k, v in variables.items()],
    }
    path.write_text(json.dumps(coll, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {path} ({len(items)} requests)")


def run(base: str) -> int:
    tag = uuid.uuid4().hex[:6]
    env = {
        "email_a": f"owner-{tag}@example.com",
        "email_a_upper": f"OWNER-{tag}@EXAMPLE.COM",
        "email_b": f"viewer-{tag}@example.com",
        "email_c": f"stranger-{tag}@example.com",
    }

    def sub(x):
        if isinstance(x, str):
            return re.sub(r"\{\{(\w+)\}\}", lambda m: env[m.group(1)], x)
        if isinstance(x, dict):
            return {k: sub(v) for k, v in x.items()}
        return x

    base = base.rstrip("/")
    print(f"Target: {base}\n")
    failures = skipped = 0
    with httpx.Client(base_url=base, timeout=20) as c:
        for r in REQUESTS:
            if r["n"] == 9 and "refresh" in env:
                env["old_refresh"] = env["refresh"]
            needed = set(re.findall(r"\{\{(\w+)\}\}", json.dumps([r["path"], r["body"]])))
            if r["auth"]:
                needed.add(r["auth"])
            missing = sorted(needed - env.keys())
            if missing:
                skipped += 1
                print(
                    f"SKIP {r['n']:02d} {r['method']:6} {r['name']}: "
                    f"needs {missing} from an earlier request that failed"
                )
                continue
            headers = {"Authorization": f"Bearer {env[r['auth']]}"} if r["auth"] else {}
            resp = c.request(r["method"], sub(r["path"]), json=sub(r["body"]), headers=headers)
            ok = resp.status_code == r["expect"]
            failures += not ok
            print(
                f"{'PASS' if ok else 'FAIL'} {r['n']:02d} {r['method']:6} {r['name']}: "
                f"{resp.status_code} (want {r['expect']})"
            )
            if not ok:
                print(f"     body: {resp.text[:200]!r}")
                if r["n"] == 1:
                    print(
                        "\n/health failed, so the URL is probably wrong (or the app is down). "
                        "Open BASE/health in a browser to check. Stopping."
                    )
                    return 1
            else:
                for var, field in r["save"].items():
                    if field:
                        env[var] = str(resp.json()[field])
    passed = len(REQUESTS) - failures - skipped
    print(f"\n{passed}/{len(REQUESTS)} passed, {failures} failed, {skipped} skipped")
    return 1 if failures or skipped else 0


if __name__ == "__main__":
    here = Path(__file__).parent
    if sys.argv[1:2] == ["export"]:
        export(here / "autocare_phase1.postman_collection.json")
    elif sys.argv[1:2] == ["run"]:
        sys.exit(run(sys.argv[2] if len(sys.argv) > 2 else "http://localhost:8000"))
    else:
        print(__doc__)

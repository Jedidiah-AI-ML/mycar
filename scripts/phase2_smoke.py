"""Phase 2 live smoke test (plan step 11): 1 car x 1 day against a real deployment.

    cd simulator
    $env:AUTOCARE_ADMIN_EMAIL = "you@example.com"; $env:AUTOCARE_ADMIN_PASSWORD = "..."
    uv run python ../scripts/phase2_smoke.py https://mycar-api-production.up.railway.app

Production has no simulation clock, so this plays the most realistic case: the car drove
for the past day **without coverage**, every batch waited in the device's flash, and now
the backlog uploads in one go with a correct clock (exactly what the gateway's skew
correction and late-data handling are for). Then:
- a few batches are sent twice straight away (lost acks), and
- the whole day is replayed shuffled.
The admin telemetry endpoint must then show every reading once, 0 duplicates, and the
day's batch count. The production rate limit (60 requests/min) is respected, so a day
takes a few minutes. About 70,000 readings (~7 MB) land in the database.

Creates one owner (with location consent), one car and one device. `--cleanup` deletes
the car and the account afterwards (readings go with the account purge, 30 days later).
"""

import argparse
import gzip
import json
import os
import random
import sys
import time
import urllib.error
import urllib.request
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "simulator"))
sys.path.insert(0, str(REPO / "scripts"))

from phase2_gate import expected_readings  # noqa: E402

from autocare_sim.device import Device  # noqa: E402
from autocare_sim.network import Network  # noqa: E402
from autocare_sim.run import LAGOS, simulate  # noqa: E402

PID = "toyota_corolla_1_8_2009"
PASSWORD = "smoke-test-password-1"  # throwaway owner account created by this script


class Api:
    def __init__(self, base: str):
        self.base = base.rstrip("/")
        self.requests = 0
        self.waited_s = 0.0

    def call(self, method, path, body=None, token=None, raw: bytes | None = None, gz=False):
        """JSON in/out. Waits and retries on 429 (the device would do the same)."""
        while True:
            data = raw if raw is not None else (json.dumps(body).encode() if body is not None
                                                  else None)  # fmt: skip
            req = urllib.request.Request(self.base + path, data=data, method=method)  # noqa: S310
            req.add_header("Content-Type", "application/json")
            if gz:
                req.add_header("Content-Encoding", "gzip")
            if token:
                req.add_header("Authorization", f"Bearer {token}")
            self.requests += 1
            try:
                with urllib.request.urlopen(req, timeout=60) as r:  # noqa: S310
                    out = r.read()
                    return json.loads(out) if out else {}
            except urllib.error.HTTPError as e:
                if e.code == 429:
                    wait = float(e.headers.get("Retry-After", "2"))
                    self.waited_s += wait
                    time.sleep(wait)
                    continue
                raise RuntimeError(f"{method} {path}: {e.code} {e.read()[:300]!r}") from None


def main() -> int:  # noqa: PLR0915 - a linear script reads best top to bottom
    ap = argparse.ArgumentParser()
    ap.add_argument("base", help="e.g. https://mycar-api-production.up.railway.app")
    ap.add_argument("--seed", type=int, default=11)
    ap.add_argument("--cleanup", action="store_true", help="delete the car and account after")
    args = ap.parse_args()
    admin_email = os.environ.get("AUTOCARE_ADMIN_EMAIL")
    admin_password = os.environ.get("AUTOCARE_ADMIN_PASSWORD")
    if not admin_email or not admin_password:
        print("Set AUTOCARE_ADMIN_EMAIL and AUTOCARE_ADMIN_PASSWORD (an is_admin account).")
        return 2
    api = Api(args.base)
    t_start = time.time()

    print("1/5 owner, car, device ...", flush=True)
    admin = api.call("POST", "/auth/login", {"email": admin_email, "password": admin_password})
    admin = admin["access_token"]
    email = f"smoke-{uuid.uuid4().hex[:6]}@example.com"
    api.call("POST", "/auth/register", {"email": email, "password": PASSWORD,
                                        "display_name": "smoke test",
                                        "consents": ["service_processing", "location"]})
    owner = api.call("POST", "/auth/login", {"email": email, "password": PASSWORD})["access_token"]
    vid = api.call("POST", "/vehicles", {"make": "Toyota", "model": "Corolla", "year": 2009},
                   owner)["id"]  # fmt: skip
    dev = api.call("POST", "/admin/devices", {}, admin)
    api.call("POST", f"/vehicles/{vid}/device",
             {"device_id": dev["id"], "claim_code": dev["claim_code"]}, owner)  # fmt: skip
    print(f"    owner {email}, vehicle {vid}, device {dev['id']}")

    print("2/5 simulating the past day, no coverage (everything waits in flash) ...", flush=True)
    now = datetime.now(UTC)
    start = (now - timedelta(days=1, hours=1)).astimezone(LAGOS).replace(
        hour=0, minute=0, second=0, microsecond=0)  # fmt: skip
    tl = simulate(PID, days=1, seed=args.seed, start=start)
    tl.trips = [t for t in tl.trips if t.end < now - timedelta(minutes=5)]  # only the past
    net = Network.perfect()
    net.enabled = True
    net.outages = [(int(start.timestamp() * 1000) - 86_400_000, int(now.timestamp() * 1000))]
    device = Device(dev["id"], tl, seed=args.seed, network=net, clock_faults=False)
    run_ = device.run(drain_days=0)
    backlog = [*device.crit_queue, *device.queue]
    print(f"    {len(tl.trips)} trips, {len(run_.produced)} batches, "
          f"{len(run_.criticals)} critical packets waiting")  # fmt: skip

    def body(item) -> bytes:
        doc = dict(item.doc)
        doc["sent_at"] = int(time.time() * 1000)  # device clock is correct: now
        if item.kind == "batch":
            doc["gnss_time"] = None
            doc["net"] = {"rssi": -79}
        return gzip.compress(json.dumps(doc, separators=(",", ":")).encode())

    print("3/5 coverage is back: uploading the backlog ...", flush=True)
    path = {"batch": "/ingest/freematics_one_plus/batches",
            "critical": "/ingest/freematics_one_plus/critical"}  # fmt: skip
    token = dev["token"]
    first_dups = retry_dups = 0
    for i, item in enumerate(backlog):
        ack = api.call("POST", path[item.kind], raw=body(item), token=token, gz=True)
        first_dups += bool(ack.get("dup"))
        if i % 10 == 0:  # the ack got lost: the device sends the same batch again
            retry_dups += bool(api.call("POST", path[item.kind], raw=body(item), token=token,
                                        gz=True).get("dup"))  # fmt: skip
    n_retried = (len(backlog) + 9) // 10

    print("4/5 replaying the whole day, shuffled ...", flush=True)
    shuffled = backlog[:]
    random.Random(args.seed).shuffle(shuffled)
    replay_dups = sum(
        bool(api.call("POST", path[it.kind], raw=body(it), token=token, gz=True).get("dup"))
        for it in shuffled
    )

    print("5/5 checking what was stored (waiting for trip assembly, ~1-2 min) ...", flush=True)
    for _ in range(36):  # assembly is debounced ~60 s after the last batch of a trip
        stats = api.call("GET", f"/admin/vehicles/{vid}/telemetry", token=admin)
        if sum(stats["trips"].values()) == len(tl.trips) and "open" not in stats["trips"]:
            break
        time.sleep(5)
    want_readings = expected_readings(run_.produced, run_.criticals)
    want_positions = sum(len(b["g"]) for b in run_.produced)
    checks = [
        ("first upload: nothing seen as duplicate", first_dups == 0, f"{first_dups}"),
        ("immediate resends: all duplicates", retry_dups == n_retried,
         f"{retry_dups} / {n_retried}"),
        ("shuffled replay: all duplicates", replay_dups == len(backlog),
         f"{replay_dups} / {len(backlog)}"),
        ("readings stored = produced", stats["readings"] == want_readings,
         f"{stats['readings']} / {want_readings}"),
        ("duplicate reading rows = 0", stats["duplicate_readings"] == 0,
         str(stats["duplicate_readings"])),
        ("batches stored = produced", stats["batches"] == len(run_.produced),
         f"{stats['batches']} / {len(run_.produced)}"),
        ("positions stored (owner consented)", stats["positions"] == want_positions,
         f"{stats['positions']} / {want_positions}"),
        ("trips assembled, none left open",
         sum(stats["trips"].values()) == len(tl.trips) and "open" not in stats["trips"],
         f"{stats['trips']} for {len(tl.trips)} trips"),
    ]  # fmt: skip
    for name, ok, detail in checks:
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}: {detail}")
    print(f"  {api.requests} requests, {api.waited_s:.0f} s waiting on the rate limit, "
          f"{(time.time() - t_start) / 60:.1f} min")  # fmt: skip
    if args.cleanup:
        api.call("DELETE", f"/vehicles/{vid}", token=owner)
        api.call("DELETE", "/me", token=owner)
        print("  cleaned up: car and account deleted (data purged in 30 days)")
    passed = all(ok for _, ok, _ in checks)
    print(f"\n{'SMOKE TEST PASSED' if passed else 'SMOKE TEST FAILED'}")
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())

"""Writes backend/tests/data/sim_trips.json: simulated Lagos trips as the device sends
them (speed samples, GPS fixes) with the simulator's TRUE distance, for the backend's
distance tests (the backend can't import the simulator, ADR-007).

    cd simulator; uv run python ../scripts/make_distance_fixture.py
"""

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "simulator"))

from autocare_sim.device import Device  # noqa: E402
from autocare_sim.run import simulate  # noqa: E402

out = []
for pid, seed in (("toyota_corolla_1_8_2009", 40), ("lexus_rx350_3_5_2010", 41)):
    tl = simulate(pid, days=3, seed=seed)
    run_ = Device("FM1-0001", tl, seed=seed, clock_faults=False).run(drain_days=0)
    for n, tr in enumerate(tl.trips, start=1):
        if not 600 <= tr.duration_s <= 1800 or len(out) >= 3:
            continue
        speed, fixes = [], []
        for b in run_.produced:
            if b["trip"] == n:
                speed += [[b["t0"] + dt, v] for dt, v in b["s"].get("speed", [])]
                fixes += [[b["t0"] + dt, lat, lon, spd] for dt, lat, lon, spd in b["g"]]
        speed.sort()
        fixes.sort()
        out.append({"car": pid, "true_km": round(tr.out.distance_km, 6),
                    "speed": speed, "fixes": fixes})  # fmt: skip
path = REPO / "backend/tests/data/sim_trips.json"
path.write_text(json.dumps(out, separators=(",", ":")) + "\n", encoding="utf-8", newline="\n")
print(f"wrote {path.relative_to(REPO)}: {len(out)} trips, "
      f"{sum(len(t['speed']) for t in out)} speed samples, {path.stat().st_size // 1024} KB")

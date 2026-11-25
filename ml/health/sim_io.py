"""Turn the simulator's device output into per-trip readings, the way the backend sees them.

The simulated Freematics device posts batches: `t0` (device ms), `s` {signal: [[dt_ms, v]]},
`ev` (events, incl. the 50 Hz cranking trace). Times come out in seconds from the trip's
first sample, like the backend's readings for one trip. GPS and motion aren't needed here.
"""

from collections import defaultdict

import numpy as np

CRANK = "crank_v"


def trips_from_run(run_) -> dict[int, dict[str, tuple[np.ndarray, np.ndarray]]]:
    pts: dict[int, dict[str, list]] = defaultdict(lambda: defaultdict(list))
    for b in run_.produced:
        trip, t0 = b["trip"], b["t0"]
        for name, rows in b["s"].items():
            pts[trip][name] += [(t0 + dt, v) for dt, v in rows]
        for ev in b.get("ev", []):
            if ev.get("type") == "crank":
                rate = float(ev.get("rate_hz", 50.0))
                start = t0 + ev["t"]
                pts[trip][CRANK] += [(start + 1000.0 * i / rate, v)
                                     for i, v in enumerate(ev["volts"])]  # fmt: skip
    out = {}
    for trip, sigs in pts.items():
        t_first = min(min(p[0] for p in rows) for rows in sigs.values())
        out[trip] = {}
        for name, rows in sigs.items():
            rows.sort()
            t = (np.array([r[0] for r in rows], float) - t_first) / 1000.0
            out[trip][name] = (t, np.array([r[1] for r in rows], float))
    return out

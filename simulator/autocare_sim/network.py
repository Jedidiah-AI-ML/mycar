"""Cellular coverage and what happens to each upload attempt.

Coverage is a list of outage windows drawn up front for the whole simulated period:
frequent short drop-outs plus occasional hours-long dead zones (SEED P7). Each upload
attempt while covered can still fail two ways:
- the request is lost before the server sees it (device retries, no harm done);
- the server stores it but the reply is lost, so the device resends the same batch and
  the server sees a *duplicate*. That is what idempotent ingestion exists for.
"""

import bisect
import math
from dataclasses import dataclass, field

import numpy as np

from autocare_sim.config import device_config, lognormal_from_pcts

OK, LOST, ACK_LOST, NO_SIGNAL = "ok", "lost", "ack_lost", "no_signal"


@dataclass
class Network:
    rng: np.random.Generator
    enabled: bool = True  # False: always covered, nothing ever lost
    outages: list[tuple[int, int]] = field(default_factory=list)  # [start_ms, end_ms)
    cfg: dict = field(default_factory=lambda: device_config()["network"])

    @classmethod
    def perfect(cls) -> "Network":
        return cls(rng=np.random.default_rng(0), enabled=False)

    @classmethod
    def lagos(cls, start_ms: int, end_ms: int, seed: int) -> "Network":
        net = cls(rng=np.random.default_rng([seed, 7]))
        net.outages = net._draw_outages(start_ms, end_ms)
        return net

    def _draw_outages(self, start_ms: int, end_ms: int) -> list[tuple[int, int]]:
        c, rng, out = self.cfg, np.random.default_rng([int(self.rng.integers(1 << 31)), 1]), []
        hours = (end_ms - start_ms) / 3_600_000
        for rate_per_h, pcts, unit_s in (
            (c["outage_per_hour"], c["outage_s"], 1),
            (c["long_outage_per_day"] / 24, c["long_outage_h"], 3600),
        ):
            mu, sigma = lognormal_from_pcts(pcts)
            for _ in range(int(rng.poisson(rate_per_h * hours))):
                t = int(rng.uniform(start_ms, end_ms))
                dur = float(np.exp(rng.normal(mu, sigma))) * unit_s
                out.append((t, t + int(dur * 1000)))
        # Merge overlaps so `covered()` is one binary search.
        out.sort()
        merged: list[tuple[int, int]] = []
        for a, b in out:
            if merged and a <= merged[-1][1]:
                merged[-1] = (merged[-1][0], max(merged[-1][1], b))
            else:
                merged.append((a, b))
        return merged

    def covered(self, t_ms: int) -> bool:
        if not self.enabled:
            return True
        i = bisect.bisect_right(self.outages, (t_ms, math.inf)) - 1
        return not (i >= 0 and self.outages[i][0] <= t_ms < self.outages[i][1])

    def attempt(self, t_ms: int) -> str:
        if not self.covered(t_ms):
            return NO_SIGNAL
        if not self.enabled:
            return OK
        u = self.rng.random()
        if u < self.cfg["request_lost_p"]:
            return LOST
        if u < self.cfg["request_lost_p"] + self.cfg["ack_lost_p"]:
            return ACK_LOST
        return OK

    def backoff_s(self, attempts: int) -> float:
        lo, hi = self.cfg["retry_s"]
        return min(lo * 2 ** max(attempts - 1, 0), hi)

    def rssi(self) -> int:
        lo, hi = self.cfg["rssi_dbm"]
        return int(self.rng.integers(lo, hi + 1))

    def outage_share(self, start_ms: int, end_ms: int) -> float:
        total = sum(max(0, min(b, end_ms) - max(a, start_ms)) for a, b in self.outages)
        return total / max(end_ms - start_ms, 1)

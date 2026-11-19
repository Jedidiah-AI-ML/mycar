"""Per-device request rate limit: a token bucket held in memory.

Each device has a bucket of `rate_per_min` tokens that refills continuously. A request
takes one; an empty bucket means 429 with Retry-After. A device draining a backlog after
an outage just slows down to the refill rate (its retry logic already backs off).

In memory, so it's per API process. That is right for v1 (one replica, PRD scope: no
Redis). With several replicas each would allow the full rate; revisit then.
"""

import time
from dataclasses import dataclass


@dataclass
class _Bucket:
    tokens: float
    updated: float


class RateLimiter:
    def __init__(self, rate_per_min: int, clock=time.monotonic):
        self.capacity = float(rate_per_min)
        self.per_s = rate_per_min / 60.0
        self.clock = clock
        self._buckets: dict[str, _Bucket] = {}

    def take(self, key: str) -> float:
        """0 if allowed, else seconds until the next token."""
        now = self.clock()
        b = self._buckets.get(key) or _Bucket(self.capacity, now)
        b.tokens = min(self.capacity, b.tokens + (now - b.updated) * self.per_s)
        b.updated = now
        self._buckets[key] = b
        if b.tokens >= 1:
            b.tokens -= 1
            return 0.0
        return (1 - b.tokens) / self.per_s

    def reset(self) -> None:
        self._buckets.clear()

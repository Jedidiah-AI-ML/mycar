"""An in-memory stand-in for the gateway: stores each batch key once, acks like the server.

Used by the tests and the data-budget run. Mirrors ADR-006: a repeated key is
acknowledged with `dup: true` and nothing is stored again.
"""

import gzip
import json
from dataclasses import dataclass, field


@dataclass
class MemorySink:
    received: list[tuple[int, str, dict]] = field(default_factory=list)  # every request
    batches: dict[tuple, dict] = field(default_factory=dict)  # unique, by key
    criticals: list[dict] = field(default_factory=list)
    commands: list[dict] = field(default_factory=list)  # handed out with the next ack
    arrival: list[tuple] = field(default_factory=list)  # keys in first-arrival order

    def post(self, kind: str, body: bytes, true_ms: int) -> dict:
        doc = json.loads(gzip.decompress(body))
        self.received.append((true_ms, kind, doc))
        dup = False
        if kind == "critical":
            self.criticals.append(doc)
        else:
            key = (doc["device_id"], doc["boot"], doc["trip"], doc["seq"])
            dup = key in self.batches
            if not dup:
                self.batches[key] = doc
                self.arrival.append(key)
        cmds, self.commands = self.commands, []
        return {"ok": True, "dup": dup, "server_time": true_ms, "cmds": cmds}

    @property
    def duplicates(self) -> int:
        return sum(1 for _, k, _ in self.received if k == "batch") - len(self.batches)

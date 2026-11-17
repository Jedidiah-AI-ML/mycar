"""HTTPS client that talks to the real gateway exactly like the AutoCare firmware will.

    sink = FreematicsHttpSink("https://<api>", "FM1-0001", secret)
    Device("FM1-0001", tl, seed=1, sink=sink).run()

- `POST {base}/ingest/freematics_one_plus/batches` (or `/critical`)
- `Content-Type: application/json`, `Content-Encoding: gzip`
- `Authorization: Bearer <device_id>.<secret>` (ADR-006)
- returns the parsed ack (`ok`, `dup`, `server_time`, `cmds`)

Standard library only (no new dependency). The gateway itself arrives in step 8.
"""

import json
import urllib.error
import urllib.request

DEVICE_TYPE = "freematics_one_plus"


class FreematicsHttpSink:
    def __init__(self, base_url: str, device_id: str, secret: str, timeout_s: float = 20.0):
        if not base_url.startswith(("https://", "http://localhost", "http://127.0.0.1")):
            raise ValueError("use https:// (plain http only for localhost)")
        self.base = base_url.rstrip("/")
        self.auth = f"Bearer {device_id}.{secret}"
        self.timeout_s = timeout_s

    def post(self, kind: str, body: bytes, true_ms: int | None = None) -> dict:
        path = "batches" if kind == "batch" else "critical"
        req = urllib.request.Request(  # noqa: S310 - scheme checked in __init__
            f"{self.base}/ingest/{DEVICE_TYPE}/{path}",
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Content-Encoding": "gzip",
                "Authorization": self.auth,
                "User-Agent": "autocare-sim/0.1",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:  # noqa: S310
                return json.loads(resp.read())
        except urllib.error.HTTPError as e:
            detail = e.read()[:300].decode(errors="replace")
            raise ConnectionError(f"{e.code} from gateway: {detail}") from e

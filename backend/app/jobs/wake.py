"""In-process wake-up for the embedded worker (ADR-008).

When the worker runs inside the API process, `enqueue()` calls `notify()` once the
enqueuing transaction commits, so a new job starts within milliseconds. No polling
and no LISTEN connection are needed, which lets a serverless database sleep.
"""

import asyncio

_event: asyncio.Event | None = None


def install(event: asyncio.Event | None) -> None:
    global _event
    _event = event


def notify() -> None:
    if _event is not None:
        _event.set()

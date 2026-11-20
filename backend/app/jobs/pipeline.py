"""The ordered stages a finished trip goes through.

Phase 2 added `trip.assemble`, Phase 3 `trip.summarise`. Phases 4 to 6 append theirs
here (features, indicators, anomaly scores, alerts), and each stage enqueues the next
when it's done, so nothing has to be stubbed in the meantime.
"""

STAGES: tuple[str, ...] = ("trip.assemble", "trip.summarise")


def next_stage(name: str) -> str | None:
    i = STAGES.index(name)
    return STAGES[i + 1] if i + 1 < len(STAGES) else None

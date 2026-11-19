"""The ordered stages a finished trip goes through.

Phase 2 has only `trip.assemble`. Phases 3 to 6 append their stages here (features,
indicators, anomaly scores, alerts), and each stage enqueues the next when it's done,
so nothing has to be stubbed in the meantime.
"""

STAGES: tuple[str, ...] = ("trip.assemble",)


def next_stage(name: str) -> str | None:
    i = STAGES.index(name)
    return STAGES[i + 1] if i + 1 < len(STAGES) else None

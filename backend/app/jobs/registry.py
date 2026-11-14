"""Job handler registry.

    @job("trip.process", max_attempts=5)
    async def process_trip(payload: dict, session: AsyncSession) -> None: ...

A handler's database work and the job's "succeeded" mark commit in the *same*
transaction, so a handler's effects are applied exactly once even if the worker
crashes mid-job (the job is then retried from scratch).
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

Handler = Callable[[dict, AsyncSession], Awaitable[None]]


@dataclass(frozen=True)
class JobSpec:
    name: str
    fn: Handler
    max_attempts: int


_REGISTRY: dict[str, JobSpec] = {}


def job(name: str, *, max_attempts: int = 5) -> Callable[[Handler], Handler]:
    def deco(fn: Handler) -> Handler:
        if name in _REGISTRY and _REGISTRY[name].fn is not fn:
            raise ValueError(f"job {name!r} registered twice")
        _REGISTRY[name] = JobSpec(name, fn, max_attempts)
        return fn

    return deco


def get_job(name: str) -> JobSpec | None:
    return _REGISTRY.get(name)

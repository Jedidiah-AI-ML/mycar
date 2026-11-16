"""Async SQLAlchemy engine and session dependency."""

import asyncio
import logging
import time
from collections.abc import AsyncIterator
from functools import lru_cache

from sqlalchemy import event
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.config import get_settings

log = logging.getLogger("app.db")


def make_engine(url: str, pool_size: int = 5, connect_timeout_s: float = 5.0) -> AsyncEngine:
    return create_async_engine(
        url,
        pool_size=pool_size,
        max_overflow=pool_size,
        pool_pre_ping=True,
        connect_args={"timeout": connect_timeout_s},
    )


@lru_cache
def get_engine() -> AsyncEngine:
    s = get_settings()
    return make_engine(s.database_url, s.db_pool_size, s.db_connect_timeout_s)


@lru_cache
def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(get_engine(), expire_on_commit=False)


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency. Routes commit explicitly; anything uncommitted is rolled back."""
    async with get_sessionmaker()() as session:
        yield session


class IdlePoolReleaser:
    """Closes pooled connections once the pool has been unused for `idle_s` (ADR-008).

    A connection pool normally keeps a few connections open forever. On a serverless
    Postgres that can count as activity and stop the database from sleeping. After a
    quiet minute this closes them; the next request simply opens a fresh one.
    """

    def __init__(self, engine: AsyncEngine, idle_s: float) -> None:
        self.engine = engine
        self.idle_s = idle_s
        self.releases = 0
        self._last_use = time.monotonic()
        # Pool events registered on the engine survive engine.dispose() (new pools
        # inherit the engine's listeners).
        event.listen(engine.sync_engine, "checkout", self._touch)
        event.listen(engine.sync_engine, "checkin", self._touch)

    def _touch(self, *_: object) -> None:
        self._last_use = time.monotonic()

    async def maybe_release(self) -> bool:
        pool = self.engine.pool
        idle_for = time.monotonic() - self._last_use
        if pool.checkedout() == 0 and pool.checkedin() > 0 and idle_for >= self.idle_s:
            await self.engine.dispose()  # in-flight connections are unaffected
            self.releases += 1
            log.info("released idle DB connections", extra={"idle_s": round(idle_for)})
            return True
        return False

    async def run(self, stop: asyncio.Event) -> None:
        interval = max(min(self.idle_s / 2, 30.0), 0.05)
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), interval)
            except TimeoutError:
                await self.maybe_release()

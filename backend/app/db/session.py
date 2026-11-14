"""Async SQLAlchemy engine and session dependency."""

from collections.abc import AsyncIterator
from functools import lru_cache

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.config import get_settings


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

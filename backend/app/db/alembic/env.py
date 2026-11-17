"""Alembic environment (async). The DB URL comes from app settings."""

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import create_async_engine

import app.models  # noqa: F401 - registers every model on Base.metadata
from app.core.config import get_settings
from app.db.base import Base

config = context.config
if config.config_file_name is not None and config.attributes.get("configure_logger", True):
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = Base.metadata


LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", "db", "host.docker.internal"}


def _url() -> str:
    url = config.attributes.get("database_url") or get_settings().database_url
    _refuse_remote_outside_production(url)
    return url


def _refuse_remote_outside_production(url: str) -> None:
    """A developer's `.env` can point at the hosted database. Migrating it from a laptop
    by accident changes production under the running app, so outside production Alembic
    only touches a local database unless ALEMBIC_ALLOW_REMOTE=1 is set on purpose."""
    import os

    from sqlalchemy.engine import make_url

    host = make_url(url).host or "localhost"
    if get_settings().environment == "production" or host in LOCAL_HOSTS:
        return
    if os.environ.get("ALEMBIC_ALLOW_REMOTE") == "1":
        return
    raise SystemExit(
        f"Refusing to run migrations against remote host {host!r} with "
        f"ENVIRONMENT={get_settings().environment}. Point DATABASE_URL at the local "
        "database, or set ALEMBIC_ALLOW_REMOTE=1 if you really mean the hosted one."
    )


def run_migrations_offline() -> None:
    context.configure(url=_url(), target_metadata=target_metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()


def _do_run(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata, compare_type=True)
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    engine = create_async_engine(_url(), poolclass=pool.NullPool)
    async with engine.connect() as conn:
        await conn.run_sync(_do_run)
    await engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())

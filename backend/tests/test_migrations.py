"""Migrations must round-trip on an empty database and match the models exactly."""

from alembic import command
from sqlalchemy import text

from app.db.session import get_engine
from tests.conftest import run_alembic


async def _table_count() -> int:
    async with get_engine().connect() as conn:
        return (
            await conn.execute(
                text(
                    "SELECT count(*) FROM information_schema.tables "
                    "WHERE table_schema='public' AND table_name <> 'alembic_version'"
                )
            )
        ).scalar_one()


async def test_upgrade_downgrade_upgrade():
    await get_engine().dispose()  # drop pooled connections before DDL
    await run_alembic(command.downgrade, "base")
    assert await _table_count() == 0
    await run_alembic(command.upgrade, "head")
    assert await _table_count() == 7


async def test_models_match_migrations():
    # `alembic check` raises if autogenerate would produce any new operations.
    await run_alembic(command.check)

"""Migrations must round-trip on an empty database and match the models exactly."""

from pathlib import Path

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
    assert await _table_count() == 25  # 7 core + 10 Phase 2 + 5 Phase 3 + 3 Phase 4 (0005)


async def test_models_match_migrations():
    # `alembic check` raises if autogenerate would produce any new operations.
    await run_alembic(command.check)


def test_alembic_refuses_remote_database_outside_production():
    """A laptop `.env` pointing at the hosted DB must not get migrated by accident."""
    import os
    import subprocess
    import sys

    env = {
        **os.environ,
        "DATABASE_URL": "postgresql+asyncpg://u:p@db.example.invalid/x",
        "ENVIRONMENT": "development",
    }
    env.pop("ALEMBIC_ALLOW_REMOTE", None)
    r = subprocess.run(
        [sys.executable, "-m", "alembic", "current"],
        cwd=Path(__file__).resolve().parents[1],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert r.returncode != 0
    assert "Refusing to run migrations against remote host 'db.example.invalid'" in r.stderr

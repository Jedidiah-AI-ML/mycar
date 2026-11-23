"""Test harness.

Needs the dev Postgres running:  docker compose up -d db   (host port 5433)
Each test session drops and recreates a throwaway `autocare_test` database, migrates
it to head with Alembic (the same path production uses), and truncates every table
after each test so tests can't leak state into each other.
"""

import asyncio
import os
from pathlib import Path

# Settings must point at the test DB *before* any app module is imported.
TEST_DB = "autocare_test"
SERVER = os.environ.get("TEST_PG_SERVER", "autocare:autocare@localhost:5433")
os.environ["DATABASE_URL"] = f"postgresql+asyncpg://{SERVER}/{TEST_DB}"
os.environ["ENVIRONMENT"] = "test"
os.environ["JWT_SECRET"] = "test-secret-not-for-production-0123456789abcdef"
os.environ["LOG_LEVEL"] = "WARNING"
os.environ["WORKER_MODE"] = "off"  # tests drive workers explicitly
os.environ["LLM_API_KEY"] = ""  # never a real (paid) LLM call from tests: fakes only

import asyncpg  # noqa: E402
import httpx  # noqa: E402
import pytest  # noqa: E402
from alembic import command  # noqa: E402
from alembic.config import Config  # noqa: E402
from pwdlib import PasswordHash  # noqa: E402
from pwdlib.hashers.argon2 import Argon2Hasher  # noqa: E402
from sqlalchemy import text  # noqa: E402

import app.core.security as security  # noqa: E402
from app.db.base import Base  # noqa: E402
from app.db.session import get_engine, get_sessionmaker  # noqa: E402

# Cheap Argon2 settings for tests only (production keeps the strong defaults). Hashes
# carry their own parameters, so verification of any hash still works.
security._hasher = PasswordHash((Argon2Hasher(time_cost=1, memory_cost=8 * 1024, parallelism=1),))

BACKEND_DIR = Path(__file__).resolve().parents[1]


def alembic_config() -> Config:
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    cfg.attributes["configure_logger"] = False
    return cfg


async def run_alembic(fn, *args) -> None:
    # env.py calls asyncio.run(), so run it in a worker thread with its own loop.
    await asyncio.to_thread(fn, alembic_config(), *args)


@pytest.fixture(scope="session", autouse=True)
async def database():
    admin = await asyncpg.connect(f"postgresql://{SERVER}/postgres")
    try:
        await admin.execute(f'DROP DATABASE IF EXISTS "{TEST_DB}" WITH (FORCE)')
        await admin.execute(f'CREATE DATABASE "{TEST_DB}"')
    finally:
        await admin.close()
    await run_alembic(command.upgrade, "head")
    yield
    await get_engine().dispose()


@pytest.fixture(autouse=True)
async def _clean_tables(database):
    yield
    names = ", ".join(t.name for t in Base.metadata.sorted_tables)
    async with get_engine().begin() as conn:
        await conn.execute(text(f"TRUNCATE {names} RESTART IDENTITY CASCADE"))


@pytest.fixture
async def session():
    async with get_sessionmaker()() as s:
        yield s


@pytest.fixture
async def client():
    from app.main import create_app

    app = create_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        c.app = app  # lets tests set dependency_overrides
        yield c

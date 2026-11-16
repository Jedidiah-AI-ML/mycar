"""Hosted Postgres URLs (Railway, Neon) must work as pasted."""

import pytest

from app.core.config import Settings

NEON = (
    "postgresql://neondb_owner:p%40ss@ep-cool-name-123456.eu-central-1.aws.neon.tech/neondb"
    "?sslmode=require&channel_binding=require"
)


def test_neon_url_is_converted_for_asyncpg():
    s = Settings(database_url=NEON)
    assert s.database_url.startswith("postgresql+asyncpg://neondb_owner:p%40ss@ep-cool-name")
    assert "ssl=require" in s.database_url
    assert "sslmode" not in s.database_url
    assert "channel_binding" not in s.database_url


def test_worker_dsn_uses_libpq_style_sslmode():
    dsn = Settings(database_url=NEON).raw_asyncpg_dsn
    assert dsn.startswith("postgresql://neondb_owner:p%40ss@")
    assert dsn.endswith("/neondb?sslmode=require")


@pytest.mark.parametrize(
    "raw",
    [
        "postgres://u:p@h:5432/db",
        "postgresql://u:p@h:5432/db",
        "postgresql+asyncpg://u:p@h:5432/db",
    ],
)
def test_plain_urls(raw):
    s = Settings(database_url=raw)
    assert s.database_url == "postgresql+asyncpg://u:p@h:5432/db"
    assert s.raw_asyncpg_dsn == "postgresql://u:p@h:5432/db"


def test_unknown_libpq_params_are_dropped():
    # asyncpg raises TypeError on unknown connect() kwargs such as `options`.
    s = Settings(database_url=NEON + "&options=endpoint%3Dep-cool&application_name=x")
    assert s.database_url.endswith("/neondb?ssl=require")


def test_copy_paste_quotes_are_stripped():
    s = Settings(database_url=f'  "{NEON}" ')
    assert s.database_url.startswith("postgresql+asyncpg://")


def test_production_refuses_default_secret():
    with pytest.raises(ValueError, match="JWT_SECRET"):
        Settings(environment="production", jwt_secret="change-me")

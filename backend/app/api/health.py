"""Health endpoints.

`/health` is liveness: the process is up (Railway's healthcheck uses it).
`/health/ready` is readiness: the database answers and its schema is at the latest
Alembic revision. If either fails it returns 503 so a bad deploy is visible.
Adapted from vpm-backend's `health.py` (Redis check removed, readiness added).
"""

from functools import lru_cache
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory
from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.core.config import get_settings
from app.db.session import get_engine

router = APIRouter(tags=["health"])

BACKEND_DIR = Path(__file__).resolve().parents[2]


@lru_cache
def alembic_head() -> str | None:
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND_DIR / "app" / "db" / "alembic"))
    return ScriptDirectory.from_config(cfg).get_current_head()


def engine_dep() -> AsyncEngine:
    return get_engine()


@router.get("/health")
async def health() -> dict:
    s = get_settings()
    return {"status": "ok", "app": s.app_name, "environment": s.environment}


@router.get("/health/ready")
async def ready(engine: AsyncEngine = Depends(engine_dep)) -> JSONResponse:
    checks: dict[str, str] = {}
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
            checks["db"] = "ok"
            try:
                current = (
                    await conn.execute(text("SELECT version_num FROM alembic_version"))
                ).scalar_one_or_none()
            except Exception:
                current = None
    except Exception as exc:  # noqa: BLE001 - any DB failure means "not ready"
        checks["db"] = f"error: {type(exc).__name__}"
        return JSONResponse({"status": "unavailable", "checks": checks}, status_code=503)

    head = alembic_head()
    checks["migrations"] = "head" if current == head else f"behind (db={current}, head={head})"
    ok = current == head
    return JSONResponse(
        {"status": "ok" if ok else "unavailable", "checks": checks},
        status_code=200 if ok else 503,
    )

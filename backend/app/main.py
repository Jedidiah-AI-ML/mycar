"""FastAPI application factory.

Adapted from vpm-backend's `main.py`: CORS origins now come from settings, routers are
mounted at the root (`/health`, `/auth`, `/vehicles`, ...).
"""

import asyncio
import contextlib
from collections.abc import AsyncIterator

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api import (
    access,
    account,
    admin,
    auth,
    codes,
    devices,
    health,
    ingest,
    maintenance,
    vehicles,
)
from app.core.config import get_settings
from app.core.logging import RequestIdMiddleware, configure_logging
from app.db.session import IdlePoolReleaser, get_engine
from app.jobs import handlers as _handlers  # noqa: F401 - registers handlers for enqueue()
from app.jobs import wake
from app.jobs.worker import Worker


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Background tasks that live as long as the API process (ADR-008)."""
    settings = get_settings()
    stop = asyncio.Event()
    tasks: list[asyncio.Task] = []
    worker: Worker | None = None

    if settings.db_idle_release_s > 0:
        releaser = IdlePoolReleaser(get_engine(), settings.db_idle_release_s)
        tasks.append(asyncio.create_task(releaser.run(stop), name="db-idle-release"))
    if settings.worker_mode == "embedded":
        worker = Worker(max_idle_s=settings.worker_max_idle_s)
        wake.install(worker._wake)
        tasks.append(asyncio.create_task(worker.run(), name="embedded-worker"))
    try:
        yield
    finally:
        stop.set()
        if worker is not None:
            worker.stop()  # finishes the current job, then exits
            wake.install(None)
        await asyncio.gather(*tasks, return_exceptions=True)
        await get_engine().dispose()


def create_app() -> FastAPI:
    settings = get_settings()
    configure_logging(settings.log_level)

    app = FastAPI(
        title=settings.app_name,
        version="0.1.0",
        docs_url="/docs" if settings.environment != "production" else None,
        redoc_url=None,
        lifespan=lifespan,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["X-Request-ID"],
    )
    app.add_middleware(RequestIdMiddleware)

    app.include_router(health.router)
    app.include_router(auth.router)
    app.include_router(account.router)
    app.include_router(vehicles.router)
    app.include_router(access.router)
    app.include_router(admin.router)
    app.include_router(devices.admin_router)
    app.include_router(devices.router)
    app.include_router(ingest.router)
    app.include_router(maintenance.router)
    app.include_router(codes.router)
    app.include_router(codes.admin_router)
    return app


app = create_app()

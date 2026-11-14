"""FastAPI application factory.

Adapted from vpm-backend's `main.py`: CORS origins now come from settings, routers are
mounted at the root (`/health`, `/auth`, `/vehicles`, ...).
"""

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api import access, account, admin, auth, health, vehicles
from app.core.config import get_settings
from app.core.logging import RequestIdMiddleware, configure_logging
from app.jobs import handlers as _handlers  # noqa: F401 - registers handlers for enqueue()


def create_app() -> FastAPI:
    settings = get_settings()
    configure_logging(settings.log_level)

    app = FastAPI(
        title=settings.app_name,
        version="0.1.0",
        docs_url="/docs" if settings.environment != "production" else None,
        redoc_url=None,
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
    return app


app = create_app()

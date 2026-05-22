"""FastAPI entry point.

Run with: uvicorn app.api.main:app
"""

from __future__ import annotations

import logging

from fastapi import FastAPI

from .. import __version__
from ..admin.routes_jobs import router as jobs_router
from ..admin.routes_roots import router as roots_router
from ..config import get_settings
from ..paths import DB_PATH, ensure_runtime_dirs
from .routes_photos import router as photos_router

logger = logging.getLogger(__name__)


def create_app() -> FastAPI:
    settings = get_settings()
    ensure_runtime_dirs()

    app = FastAPI(
        title=settings.app.name,
        version=__version__,
        docs_url="/api/docs",
        openapi_url="/api/openapi.json",
    )

    @app.get("/healthz", tags=["meta"])
    def healthz() -> dict:
        return {
            "ok": True,
            "version": __version__,
            "db": str(DB_PATH),
            "db_exists": DB_PATH.exists(),
        }

    app.include_router(roots_router, prefix="/api")
    app.include_router(jobs_router, prefix="/api")
    app.include_router(photos_router, prefix="/api")
    return app


app = create_app()

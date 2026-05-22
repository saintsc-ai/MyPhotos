"""FastAPI entry point.

Run with: uvicorn app.api.main:app
"""

from __future__ import annotations

import logging
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from .. import __version__
from ..admin.routes_jobs import router as jobs_router
from ..admin.routes_roots import router as roots_router
from ..config import get_settings
from ..paths import DB_PATH, PROJECT_ROOT, ensure_runtime_dirs
from .routes_photos import router as photos_router

logger = logging.getLogger(__name__)

WEB_STATIC_DIR: Path = PROJECT_ROOT / "app" / "web" / "static"


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

    # API routers (registered before the static catch-all)
    app.include_router(roots_router, prefix="/api")
    app.include_router(jobs_router, prefix="/api")
    app.include_router(photos_router, prefix="/api")

    # Static gallery — mounted at root so '/' serves index.html.
    # html=True enables directory-index behaviour.
    if WEB_STATIC_DIR.exists():
        app.mount("/", StaticFiles(directory=str(WEB_STATIC_DIR), html=True), name="web")

    return app


app = create_app()

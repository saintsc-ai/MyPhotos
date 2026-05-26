"""FastAPI entry point.

Run with: uvicorn app.api.main:app
"""

from __future__ import annotations

import logging
from pathlib import Path

from fastapi import Depends, FastAPI
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from .. import __version__
from ..admin.routes_duplicates import router as duplicates_router
from ..admin.routes_jobs import router as jobs_router
from ..admin.routes_ml import router as ml_router
from ..admin.routes_roots import router as roots_router
from ..admin.routes_settings import router as settings_router
from ..admin.routes_trash import router as trash_router
from ..auth import (
    SESSION_COOKIE,
    SESSION_MAX_AGE,
    admin_users_router,
    ensure_default_admin,
    get_session_secret,
    require_admin,
    require_auth,
)
from ..auth import router as auth_router
from ..config import get_settings
from ..db import SessionLocal
from ..external import exiftool_path, ffmpeg_path
from ..paths import DB_PATH, PROJECT_ROOT, ensure_runtime_dirs
from ..shares import admin_router as shares_admin_router
from ..shares import public_router as shares_public_router
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

    # Signed-cookie sessions. Secret persists in data/session.secret.
    app.add_middleware(
        SessionMiddleware,
        secret_key=get_session_secret(),
        session_cookie=SESSION_COOKIE,
        max_age=SESSION_MAX_AGE,
        same_site="lax",
        https_only=False,  # set True once behind a TLS reverse proxy
    )

    # Seed admin / admin on first launch.
    with SessionLocal() as db:
        ensure_default_admin(db)

    @app.get("/api/config", tags=["meta"])
    def app_config() -> dict:
        """Public app configuration for the static frontends.

        Exposes just the values that branding/UI need to render correctly
        on the login page (which can't otherwise read settings since it
        runs before authentication), plus the map-to-lightbox tunables so
        admins can adjust without editing static files.
        """
        return {
            "app_name": settings.app.name,
            "display_timezone": settings.app.display_timezone,
            "map_nearby_radius_deg": settings.map.nearby_radius_deg,
            "map_nearby_limit": settings.map.nearby_limit,
        }

    @app.get("/healthz", tags=["meta"])
    def healthz() -> dict:
        # Probe pillow-heif (optional [heic] extra)
        try:
            import pillow_heif  # noqa: F401

            heic = True
        except ImportError:
            heic = False
        # Watcher heartbeat — separate process, so the only thing the
        # API can do is read the on-disk heartbeat file. Stale (or
        # missing) heartbeat indicates the watcher service is down or
        # stuck, even if `watcher.enabled=true`.
        watcher_info: dict = {"enabled": settings.watcher.enabled}
        try:
            import json
            from datetime import datetime
            from ..paths import STATE_DIR
            hb_path = STATE_DIR / "watcher.json"
            if hb_path.exists():
                data = json.loads(hb_path.read_text(encoding="utf-8"))
                alive_at = data.get("alive_at")
                age_seconds: int | None = None
                if isinstance(alive_at, str):
                    try:
                        ts = datetime.fromisoformat(alive_at.rstrip("Z"))
                        age_seconds = int((datetime.utcnow() - ts).total_seconds())
                    except ValueError:
                        pass
                watcher_info.update({
                    "alive_at": alive_at,
                    "age_seconds": age_seconds,
                    # >15s without an update → likely dead. Dispatcher
                    # writes every ~2s so the threshold is generous.
                    "stale": age_seconds is None or age_seconds > 15,
                    "watched_root_ids": data.get("watched_root_ids", []),
                    "pending_roots": data.get("pending_roots", 0),
                })
            else:
                watcher_info["stale"] = True
                watcher_info["note"] = (
                    "no heartbeat file — watcher never started, or disabled"
                )
        except Exception as e:
            watcher_info["error"] = str(e)
        return {
            "ok": True,
            "version": __version__,
            "db": str(DB_PATH),
            "db_exists": DB_PATH.exists(),
            "tools": {
                "exiftool": exiftool_path(),
                "ffmpeg": ffmpeg_path(),
                "pillow_heif": heic,
            },
            "watcher": watcher_info,
        }

    # Auth + public share routes are public (their token is the secret).
    app.include_router(auth_router, prefix="/api")
    app.include_router(shares_public_router, prefix="/api")

    # Roots + jobs are admin-only (system-level configuration).
    # Photos + share management are open to any logged-in user.
    auth_only = [Depends(require_auth)]
    admin_only = [Depends(require_admin)]
    app.include_router(roots_router, prefix="/api", dependencies=admin_only)
    app.include_router(jobs_router, prefix="/api", dependencies=admin_only)
    app.include_router(admin_users_router, prefix="/api", dependencies=admin_only)
    app.include_router(settings_router, prefix="/api", dependencies=admin_only)
    app.include_router(ml_router, prefix="/api", dependencies=admin_only)
    app.include_router(trash_router, prefix="/api", dependencies=admin_only)
    app.include_router(duplicates_router, prefix="/api", dependencies=admin_only)
    app.include_router(photos_router, prefix="/api", dependencies=auth_only)
    app.include_router(shares_admin_router, prefix="/api", dependencies=auth_only)

    # Static gallery — login.html is here too, so the mount stays public.
    # The frontend redirects to /login.html when /api/auth/me returns 401.
    if WEB_STATIC_DIR.exists():
        app.mount("/", StaticFiles(directory=str(WEB_STATIC_DIR), html=True), name="web")

    return app


app = create_app()

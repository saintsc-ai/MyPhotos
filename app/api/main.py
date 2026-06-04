"""FastAPI entry point.

Run with: uvicorn app.api.main:app
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from fastapi import Depends, FastAPI
from fastapi.staticfiles import StaticFiles
from starlette.middleware.gzip import GZipMiddleware
from starlette.middleware.sessions import SessionMiddleware

from .. import __version__
from ..admin.routes_audit import router as audit_router
from ..admin.routes_database import router as database_router
from ..admin.routes_duplicates import router as duplicates_router
from ..admin.routes_folders import router as folders_router
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

    # Disable the public Swagger/openapi mounts — they leaked the whole
    # API surface (including admin + share endpoints) to anyone who hit
    # /api/docs without authentication, which is a recon goldmine on a
    # DDNS-exposed deployment. Admin-protected re-mounts are wired up
    # below after require_admin is in scope.
    app = FastAPI(
        title=settings.app.name,
        version=__version__,
        docs_url=None,
        openapi_url=None,
    )

    # Compress JSON / HTML responses ≥1 KiB. Big wins on /api/photos
    # list payloads and the static gallery HTML; FileResponse byte
    # streams (thumbs/originals) are skipped because they're already
    # JPEG-compressed. minimum_size below 1 KiB just wastes CPU.
    app.add_middleware(GZipMiddleware, minimum_size=1024)

    # Per-request timing — adds Server-Timing so DevTools' Timing tab
    # shows the server's processing time alongside network/render, and
    # writes a WARN line for any request > 1s so slow paths surface
    # in journalctl without needing a profiler attached.
    import time as _t
    _slow_log = logging.getLogger("myphotos.slow")

    @app.middleware("http")
    async def _timing_mw(request, call_next):
        t0 = _t.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            elapsed = (_t.perf_counter() - t0) * 1000
            _slow_log.warning(
                "EXC %s %s %.0fms", request.method, request.url.path, elapsed
            )
            raise
        elapsed_ms = (_t.perf_counter() - t0) * 1000
        # Header is safe to add even on streamed FileResponse — fastapi
        # writes headers before the body iterator runs.
        try:
            response.headers["Server-Timing"] = f"total;dur={elapsed_ms:.1f}"
        except Exception:
            pass
        if elapsed_ms > 1000:
            _slow_log.warning(
                "SLOW %s %s %.0fms (status=%s)",
                request.method, request.url.path, elapsed_ms,
                getattr(response, "status_code", "?"),
            )
        return response

    # --- Security response headers ---
    # nosniff + Referrer-Policy always; X-Frame-Options / HSTS per config.
    # setdefault so a route that sets its own (e.g. a future relaxed
    # Referrer-Policy on share embeds) isn't overridden.
    _sec = settings.security

    @app.middleware("http")
    async def _security_headers_mw(request, call_next):
        response = await call_next(request)
        h = response.headers
        h.setdefault("X-Content-Type-Options", "nosniff")
        h.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        if _sec.frame_deny:
            h.setdefault("X-Frame-Options", "SAMEORIGIN")
        if _sec.hsts:
            h.setdefault(
                "Strict-Transport-Security",
                f"max-age={int(_sec.hsts_max_age)}; includeSubDomains",
            )
        return response

    # --- GeoIP country gate (optional, fail-open) ---
    # Active only when geoip2 + a GeoLite2 .mmdb are present and a mode is
    # set. Private/loopback IPs are always allowed so LAN + the reverse
    # proxy itself never get blocked. Any lookup miss → allow (never lock
    # the admin out over a bad DB).
    import ipaddress as _ipaddress

    _geo_mode = (_sec.geoip_mode or "off").strip().lower()
    _geo_countries = {c.strip().upper() for c in (_sec.geoip_countries or []) if c.strip()}
    _geo_reader = None
    if _geo_mode in ("allow", "block") and _sec.geoip_db_path:
        try:
            import geoip2.database  # type: ignore

            _geo_reader = geoip2.database.Reader(_sec.geoip_db_path)
            logger.info(
                "GeoIP gate active: mode=%s countries=%s",
                _geo_mode, sorted(_geo_countries),
            )
        except Exception as e:  # pkg missing / bad path / unreadable db
            logger.warning("GeoIP gate requested but disabled (fail-open): %s", e)
            _geo_reader = None

    def _req_ip(request) -> str:
        if _sec.trust_proxy_xff:
            xff = request.headers.get("x-forwarded-for")
            if xff:
                return xff.split(",")[0].strip()
        return request.client.host if request.client else ""

    def _ip_is_local(ip: str) -> bool:
        try:
            a = _ipaddress.ip_address(ip)
        except ValueError:
            return True  # unparseable → don't block
        return a.is_private or a.is_loopback or a.is_link_local or a.is_reserved

    if _geo_reader is not None:
        @app.middleware("http")
        async def _geoip_mw(request, call_next):
            ip = _req_ip(request)
            if not ip or _ip_is_local(ip):
                return await call_next(request)
            try:
                country = _geo_reader.country(ip).country.iso_code
            except Exception:
                return await call_next(request)  # lookup miss → fail-open
            if country is None:
                return await call_next(request)
            if _geo_mode == "allow":
                blocked = country not in _geo_countries
            else:
                blocked = country in _geo_countries
            if blocked:
                from starlette.responses import JSONResponse
                return JSONResponse(
                    {"detail": "Access denied for your region"}, status_code=403,
                )
            return await call_next(request)

    # CSRF defence — Origin/Referer check on state-changing methods.
    # same_site=lax already blocks cross-site POST in most browsers,
    # but doesn't cover top-level navigation GETs that change state
    # (e.g. bulk-download / share zip), older browser regressions, or
    # POST that re-uses a fetch with credentials=include. Origin header
    # is the strongest signal — modern browsers always send it on
    # cross-origin POSTs and never let JS forge it.
    #
    # `MYPHOTOS_TRUSTED_ORIGINS` (comma-separated full origins, e.g.
    # "https://photos.example.com,https://192.168.1.201:8888") opts in
    # additional same-site origins beyond the request's own host. Empty
    # / unset is fine for LAN deployments where the user only ever hits
    # the API from the same host the static frontend was served from.
    _csrf_safe_methods = {"GET", "HEAD", "OPTIONS"}
    _csrf_trusted = {
        o.strip().rstrip("/")
        for o in os.environ.get("MYPHOTOS_TRUSTED_ORIGINS", "").split(",")
        if o.strip()
    }

    @app.middleware("http")
    async def _csrf_mw(request, call_next):
        if request.method in _csrf_safe_methods:
            return await call_next(request)
        # Public share routes are not part of the cookie-auth surface —
        # they're token-authenticated and may be hit cross-origin (e.g.
        # iframe embed of share.html on another site). Skip CSRF
        # enforcement here so we don't break that use case.
        path = request.url.path
        if path.startswith("/api/share/"):
            return await call_next(request)
        # Login endpoint must accept cross-origin from the login page
        # served as a static file (same origin) — but we still need to
        # block scripted POSTs from evil.com. The origin check below
        # handles that.
        origin = request.headers.get("origin")
        referer = request.headers.get("referer")
        host_origin = f"{request.url.scheme}://{request.url.netloc}".rstrip("/")
        # Build the candidate origin from origin OR referer (use whichever
        # is present; both are normally sent by browsers on POST).
        candidate = None
        if origin:
            candidate = origin.rstrip("/")
        elif referer:
            # Strip path/query off the referer to compare origins only.
            from urllib.parse import urlsplit
            sp = urlsplit(referer)
            if sp.scheme and sp.netloc:
                candidate = f"{sp.scheme}://{sp.netloc}".rstrip("/")
        if candidate is None:
            # No origin AND no referer — almost always a non-browser
            # client (curl, server-side). Allow: this is the
            # original-API use case (scripts hitting the JSON API with
            # the session cookie they got from /login).
            return await call_next(request)
        if candidate == host_origin or candidate in _csrf_trusted:
            return await call_next(request)
        from starlette.responses import JSONResponse
        return JSONResponse(
            {"detail": f"CSRF: origin {candidate!r} not allowed"},
            status_code=403,
        )

    # Signed-cookie sessions. Secret persists in data/session.secret.
    # `MYPHOTOS_SECURE_COOKIE=1` flips on https_only + same_site=strict —
    # set this in the .env / systemd unit when the API is reached via
    # a TLS reverse proxy (DSM RP, Caddy, Tailscale serve, etc.). Local
    # LAN-only deploys can leave it unset.
    _secure = os.environ.get("MYPHOTOS_SECURE_COOKIE", "").strip() in ("1", "true", "yes")
    app.add_middleware(
        SessionMiddleware,
        secret_key=get_session_secret(),
        session_cookie=SESSION_COOKIE,
        max_age=SESSION_MAX_AGE,
        same_site="strict" if _secure else "lax",
        https_only=_secure,
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
            # UI default language so every page (login / gallery /
            # share / admin) can seed i18n.init() without admin perms.
            # Per-user picks live in the browser's localStorage and
            # override this.
            "default_language": settings.app.default_language,
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
        # DB block — reports the actually-resolved backend, not the
        # SQLite path constant. Previously this always returned the
        # SQLite DB_PATH regardless of whether the active backend was
        # MariaDB / PostgreSQL, which had a user thinking their
        # MariaDB migration silently rolled back. Backend name comes
        # from SQLAlchemy's dialect ("sqlite" / "mysql" /
        # "postgresql") so the report stays honest as new backends
        # are wired up. Mask the password segment so /healthz can
        # stay public-readable.
        from ..db import resolve_db_url, is_sqlite_url, engine
        from ..admin.routes_database import _mask_dsn
        _db_url = resolve_db_url()
        if is_sqlite_url(_db_url):
            db_block = {
                "backend": "sqlite",
                "path": str(DB_PATH),
                "exists": DB_PATH.exists(),
            }
        else:
            db_block = {
                "backend": engine.dialect.name,   # "mysql" | "postgresql" | ...
                "dsn": _mask_dsn(_db_url),
            }
        return {
            "ok": True,
            "version": __version__,
            "db": db_block,
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
    # Trash router runs per-endpoint guards (require_can_delete +
    # per-user scoping in P5); mount with auth_only so non-admin
    # callers can manage their own deletions.
    app.include_router(trash_router, prefix="/api", dependencies=auth_only)
    app.include_router(duplicates_router, prefix="/api", dependencies=admin_only)
    app.include_router(database_router, prefix="/api", dependencies=admin_only)
    app.include_router(audit_router, prefix="/api", dependencies=admin_only)
    # Folders router gates per-endpoint with require_can_upload /
    # require_can_delete / require_can_edit_meta_others (P1 of the
    # access-control plan). Mount with auth_only so non-admin users
    # whose flags are set can actually reach those endpoints; admins
    # bypass the flag check inside the dependency.
    app.include_router(folders_router, prefix="/api", dependencies=auth_only)
    app.include_router(photos_router, prefix="/api", dependencies=auth_only)
    app.include_router(shares_admin_router, prefix="/api", dependencies=auth_only)

    # Admin-only Swagger / OpenAPI. Mounted manually so the dependency
    # actually runs (FastAPI's docs_url= bypass would need its own
    # auth wiring). The schema route returns the live OpenAPI JSON
    # built from the current app; the docs route serves Swagger UI
    # pointed at that schema.
    from fastapi.openapi.docs import get_swagger_ui_html
    from fastapi.openapi.utils import get_openapi
    from fastapi.responses import JSONResponse as _JSONResponse

    @app.get("/api/openapi.json", include_in_schema=False, dependencies=admin_only)
    def _openapi_admin() -> _JSONResponse:
        schema = get_openapi(
            title=app.title, version=app.version, routes=app.routes,
        )
        return _JSONResponse(schema)

    @app.get("/api/docs", include_in_schema=False, dependencies=admin_only)
    def _docs_admin():
        return get_swagger_ui_html(
            openapi_url="/api/openapi.json", title=app.title + " — API docs"
        )

    # Static gallery — login.html is here too, so the mount stays public.
    # The frontend redirects to /login.html when /api/auth/me returns 401.
    if WEB_STATIC_DIR.exists():
        app.mount("/", StaticFiles(directory=str(WEB_STATIC_DIR), html=True), name="web")

    return app


app = create_app()

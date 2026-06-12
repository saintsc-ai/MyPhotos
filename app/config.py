"""Configuration loader.

Loads `config/default.toml` then merges `config/local.toml` on top.
Local file is optional during early development; required for production
(secret_key in particular).
"""

from __future__ import annotations

import tomllib
from functools import lru_cache
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from .paths import CONFIG_DIR
# Pure dataclass/string module (no numpy/onnx), safe to import here for the
# shipped default of ml.exclusive_category_groups.
from .worker_ml.categories import DEFAULT_EXCLUSIVE_GROUPS


class ServerConfig(BaseModel):
    host: str = "127.0.0.1"
    port: int = 8080
    workers: int = 2


class WorkerConfig(BaseModel):
    concurrency: int = 4
    idle_poll_seconds: int = 2
    job_lease_seconds: int = 600
    # Threads for the ML worker (YOLO / CLIP / faces). ONNX kernels are
    # heavier per call than the indexing worker, so default lower.
    ml_concurrency: int = 2


class ScannerConfig(BaseModel):
    ignore_dirs: list[str] = Field(default_factory=list)
    ignore_files: list[str] = Field(default_factory=list)
    image_extensions: list[str] = Field(default_factory=list)
    video_extensions: list[str] = Field(default_factory=list)


class ThumbnailsConfig(BaseModel):
    sizes: list[int] = Field(default_factory=lambda: [256, 1024])
    quality: int = 85


class ExifConfig(BaseModel):
    extractor_chain: list[str] = Field(default_factory=lambda: ["pillow", "exiftool"])
    required_fields: list[str] = Field(default_factory=lambda: ["taken_at"])


class AppMeta(BaseModel):
    name: str = "MyPhotos"
    display_timezone: str = "UTC"
    # Default UI language for users who haven't picked one in the
    # browser yet. Two-letter or BCP-47 short tag — must be one of the
    # codes shipped under app/web/static/i18n/.
    default_language: str = "ko"


class SecurityConfig(BaseModel):
    secret_key: str = "CHANGE_ME_BEFORE_FIRST_RUN"

    # --- Account lockout (per-username, DB-backed) ---
    # After this many *consecutive* failed logins, the account is locked
    # for `lockout_minutes`. A successful login resets the counter. 0 in
    # either field disables lockout (the per-IP throttle still applies).
    lockout_threshold: int = 10
    lockout_minutes: int = 15

    # --- Security response headers ---
    # X-Content-Type-Options:nosniff and Referrer-Policy are always sent.
    # frame_deny adds X-Frame-Options:SAMEORIGIN (clickjacking). hsts adds
    # Strict-Transport-Security — only enable when served over HTTPS (a
    # reverse proxy / Tailscale / Caddy), otherwise it's ignored on HTTP.
    frame_deny: bool = True
    hsts: bool = False
    hsts_max_age: int = 31_536_000   # 1 year

    # --- GeoIP country gate ---
    # Optional. Needs the `geoip2` package and a MaxMind GeoLite2-Country
    # .mmdb file (free, requires a MaxMind account). When the package or DB
    # is missing the gate silently disables (fail-open) so you can't lock
    # yourself out by misconfiguring it.
    #   geoip_mode = "off"   → disabled
    #   geoip_mode = "allow" → only `geoip_countries` may connect
    #   geoip_mode = "block" → `geoip_countries` are refused
    # Private/loopback IPs (LAN, the reverse proxy itself) are always
    # allowed. trust_proxy_xff: read the client IP from X-Forwarded-For
    # (set true ONLY behind a trusted reverse proxy that overwrites it).
    geoip_mode: str = "off"
    geoip_db_path: str = ""
    geoip_countries: list[str] = Field(default_factory=list)
    trust_proxy_xff: bool = False


class PathOverrides(BaseModel):
    data_dir: str | None = None
    exiftool: str | None = None
    ffmpeg: str | None = None


class LoggingConfig(BaseModel):
    level: str = "INFO"
    rotate_bytes: int = 10_485_760
    rotate_keep: int = 5


class MapConfig(BaseModel):
    """Tunables for the map → lightbox flow.

    `nearby_radius_deg` is the lat/lng half-extent of the bounding box used
    by /api/photos/nearby (0.005° ≈ 500m, 0.01° ≈ 1km, 0.05° ≈ 5km).
    `nearby_limit` caps the resulting list (also caps the lightbox filmstrip
    contents in that mode).
    """

    nearby_radius_deg: float = 0.005
    nearby_limit: int = 100


class VideoConfig(BaseModel):
    """Web-playable H.264 proxy cache (data/proxies).

    Proxies are disposable — they regenerate on demand — so the cache is
    bounded: once the total exceeds the cap, least-recently-*played*
    proxies are evicted (each serve bumps the file mtime). 0 = unbounded.
    """
    proxy_cache_max_gb: float = 50.0


class WatcherConfig(BaseModel):
    # Set to true to enable filesystem watchdog (inotify on Linux).
    # Off by default so existing installs aren't surprised by a new
    # process or by /proc/sys/fs/inotify watch consumption.
    enabled: bool = False
    # Quiet period after the last event before a root scan is queued.
    # Coalesces bursts (folder copy, batch rename) into one rescan.
    debounce_seconds: int = 30
    # Periodic re-check that observers match enabled roots (new roots
    # picked up, deleted roots unsubscribed). Cheap, just a DB read.
    reconcile_roots_seconds: int = 60
    # Catch-up scan when the watcher starts, so changes that happened
    # while it was down get a chance to be reconciled.
    initial_scan_on_start: bool = True


class MlConfig(BaseModel):
    """ML pipeline (object/CLIP/face classification + OCR).

    auto_enqueue: when on, newly-indexed photos get their ML + OCR jobs
    queued automatically right after the thumbnail is ready — no manual
    "ML 자동 분류" run needed. Off by default: do the initial backlog
    manually (it's heavy), then turn this on so new arrivals are handled
    automatically. Read by the indexing worker, so a change needs a
    worker restart.

    exclusive_category_groups: lists of mutually-exclusive CLIP scene tags.
    Within each group only the single highest-scoring match survives (e.g.
    실내 vs 야외 on a landscape → keep 야외). Read by the ML worker when it
    auto-tags, so a change needs a worker restart. See
    worker_ml/categories.py for the shipped defaults + rationale.
    """
    auto_enqueue: bool = False
    exclusive_category_groups: list[list[str]] = Field(
        default_factory=lambda: [list(g) for g in DEFAULT_EXCLUSIVE_GROUPS]
    )


class OcrConfig(BaseModel):
    """OCR text extraction (RapidOCR / onnxruntime).

    Opt-in ML stage — the admin enqueues it. Bundled models cover
    Latin/Chinese; for Korean set rec_model_path + rec_keys_path to a
    Korean PP-OCR rec model (ONNX) + its dict (see docs). Empty paths →
    bundled models. min_score drops low-confidence lines; max_chars caps
    the text stored/indexed per photo.
    """
    min_score: float = 0.5
    max_chars: int = 4000
    # rapidocr v3: language whose model is auto-downloaded (e.g. "korean",
    # "japan", "ch", "en"). Ignored by the v1 backend (which uses the
    # explicit *_model_path overrides below).
    lang: str = "korean"
    det_model_path: str = ""
    rec_model_path: str = ""
    rec_keys_path: str = ""
    cls_model_path: str = ""


class DedupConfig(BaseModel):
    """Duplicate (same sha256) handling.

    - auto_cleanup: a periodic worker tick that runs the same "keep the
      earliest, trash the rest" sweep as the admin 중복 제거 button. Off by
      default so existing installs don't start trashing on their own.
    - skip_ingest: when the indexer hashes a newly-discovered file and an
      ACTIVE photo already has that sha256, trash the incoming copy (keeps
      the lowest id). Catches duplicates that arrive outside the
      authenticated /upload flow (e.g. PhotoSync over SMB) — the upload
      endpoint already rejects same-sha files up-front.
    """
    auto_cleanup: bool = False
    auto_cleanup_interval_hours: int = 24
    skip_ingest: bool = False


class DatabaseConfig(BaseModel):
    # Empty string / unset → fall back to the bundled SQLite catalog at
    # data/catalog.db (the default, recommended path).
    # MariaDB / MySQL example (install the [mariadb] extra first):
    #   url = "mysql+pymysql://user:pass@host:3306/myphotos?charset=utf8mb4"
    # PostgreSQL example (install the [postgres] extra first):
    #   url = "postgresql+psycopg://user:pass@host:5432/myphotos"
    # See docs/operations/external-db.md for the full setup, including
    # the SQLite-only feature caveats (FTS5, strftime, GROUP_CONCAT) that
    # apply to every non-SQLite backend.
    url: str = ""


class Settings(BaseModel):
    app: AppMeta = Field(default_factory=AppMeta)
    server: ServerConfig = Field(default_factory=ServerConfig)
    worker: WorkerConfig = Field(default_factory=WorkerConfig)
    scanner: ScannerConfig = Field(default_factory=ScannerConfig)
    thumbnails: ThumbnailsConfig = Field(default_factory=ThumbnailsConfig)
    exif: ExifConfig = Field(default_factory=ExifConfig)
    security: SecurityConfig = Field(default_factory=SecurityConfig)
    paths: PathOverrides = Field(default_factory=PathOverrides)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)
    map: MapConfig = Field(default_factory=MapConfig)
    video: VideoConfig = Field(default_factory=VideoConfig)
    database: DatabaseConfig = Field(default_factory=DatabaseConfig)
    watcher: WatcherConfig = Field(default_factory=WatcherConfig)
    dedup: DedupConfig = Field(default_factory=DedupConfig)
    ml: MlConfig = Field(default_factory=MlConfig)
    ocr: OcrConfig = Field(default_factory=OcrConfig)


def _read_toml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    with path.open("rb") as f:
        return tomllib.load(f)


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    default = _read_toml(CONFIG_DIR / "default.toml")
    local = _read_toml(CONFIG_DIR / "local.toml")
    merged = _deep_merge(default, local)
    return Settings.model_validate(merged)


# ---------- runtime editing (used by /api/admin/settings) ----------

LOCAL_TOML_PATH: Path = CONFIG_DIR / "local.toml"


def _toml_literal(v: Any) -> str:
    """Serialize a primitive (str / int / float / bool / list of primitives) as a TOML value."""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return repr(v)
    if isinstance(v, str):
        # Escape backslashes and double-quotes; basic TOML strings are sufficient.
        return '"' + v.replace("\\", "\\\\").replace('"', '\\"') + '"'
    if isinstance(v, list):
        return "[" + ", ".join(_toml_literal(x) for x in v) + "]"
    if v is None:
        return '""'
    raise TypeError(f"Unsupported TOML value: {type(v).__name__}")


def _dump_toml(data: dict[str, Any]) -> str:
    """Tiny TOML writer good enough for our shape (top-level sections of primitives)."""
    parts: list[str] = [
        "# MyPhotos host overrides — managed via the admin UI (/admin.html).",
        "# Manual edits are preserved on the next admin save.",
        "",
    ]
    # Anything that isn't a section table gets emitted first (we don't currently use this).
    for k, v in data.items():
        if not isinstance(v, dict):
            parts.append(f"{k} = {_toml_literal(v)}")
    for section, body in data.items():
        if not isinstance(body, dict):
            continue
        parts.append(f"[{section}]")
        for k, v in body.items():
            parts.append(f"{k} = {_toml_literal(v)}")
        parts.append("")
    return "\n".join(parts).rstrip() + "\n"


def update_local_settings(updates: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Merge `updates` into config/local.toml and invalidate the cached Settings.

    `updates` shape: {section: {key: value, ...}, ...}. Sections/keys not in
    `updates` are preserved untouched. Returns the new merged local.toml contents.
    """
    LOCAL_TOML_PATH.parent.mkdir(parents=True, exist_ok=True)
    current = _read_toml(LOCAL_TOML_PATH)
    merged = _deep_merge(current, updates)
    LOCAL_TOML_PATH.write_text(_dump_toml(merged), encoding="utf-8")
    get_settings.cache_clear()
    return merged

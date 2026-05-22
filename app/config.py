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


class ServerConfig(BaseModel):
    host: str = "127.0.0.1"
    port: int = 8080
    workers: int = 2


class WorkerConfig(BaseModel):
    concurrency: int = 4
    idle_poll_seconds: int = 2
    job_lease_seconds: int = 600


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


class SecurityConfig(BaseModel):
    secret_key: str = "CHANGE_ME_BEFORE_FIRST_RUN"


class PathOverrides(BaseModel):
    data_dir: str | None = None
    exiftool: str | None = None
    ffmpeg: str | None = None


class LoggingConfig(BaseModel):
    level: str = "INFO"
    rotate_bytes: int = 10_485_760
    rotate_keep: int = 5


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

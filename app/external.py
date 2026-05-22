"""External binary resolution.

Looks up exiftool / ffmpeg in this order:
  1. Config override (config/local.toml → [paths] exiftool / ffmpeg)
  2. vendor/<os>-<arch>/<name>[.exe]   (bundled with the repo)
  3. $PATH (system install)

Returns None if no working binary is found — callers must handle absence
gracefully (the EXIF chain skips ExifTool, video thumbs are marked as
unsupported, etc.).
"""

from __future__ import annotations

import logging
import platform
import shutil
import subprocess
from functools import lru_cache
from pathlib import Path

from .config import get_settings
from .paths import VENDOR_DIR

log = logging.getLogger(__name__)


def _platform_dir() -> str:
    system = platform.system()
    machine = platform.machine().lower()
    os_part = {"windows": "windows", "linux": "linux", "darwin": "macos"}.get(
        system.lower(), system.lower()
    )
    arch_part = {
        "amd64": "x64",
        "x86_64": "x64",
        "aarch64": "arm64",
        "arm64": "arm64",
    }.get(machine, machine)
    return f"{os_part}-{arch_part}"


def _resolve(name: str, override: str | None) -> str | None:
    if override:
        p = Path(override)
        if p.exists():
            return str(p)
        log.warning("config override for %s points to missing file: %s", name, override)

    exe = f"{name}.exe" if platform.system() == "Windows" else name
    bundled = VENDOR_DIR / _platform_dir() / exe
    if bundled.exists():
        return str(bundled)

    on_path = shutil.which(name)
    if on_path:
        return on_path

    return None


def _probe(path: str | None, args: list[str]) -> bool:
    if not path:
        return False
    try:
        subprocess.run(
            [path, *args],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=5,
            check=True,
        )
        return True
    except (subprocess.SubprocessError, OSError):
        return False


@lru_cache(maxsize=1)
def exiftool_path() -> str | None:
    s = get_settings()
    path = _resolve("exiftool", s.paths.exiftool)
    if path and _probe(path, ["-ver"]):
        log.info("exiftool: %s", path)
        return path
    log.info("exiftool: not available")
    return None


@lru_cache(maxsize=1)
def ffmpeg_path() -> str | None:
    s = get_settings()
    path = _resolve("ffmpeg", s.paths.ffmpeg)
    if path and _probe(path, ["-version"]):
        log.info("ffmpeg: %s", path)
        return path
    log.info("ffmpeg: not available")
    return None

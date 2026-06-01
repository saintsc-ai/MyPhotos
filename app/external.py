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
from pathlib import Path

from .config import get_settings
from .paths import VENDOR_DIR

log = logging.getLogger(__name__)

# Positive-only resolution cache. lru_cache would memoise the
# `not-found` answer too, which means a worker that booted before the
# user installed exiftool/ffmpeg would keep returning None forever
# even after the binaries appeared on disk. Here we only cache hits;
# misses re-probe on every call so a freshly-dropped vendor binary is
# picked up without a worker restart. Probe is cheap (one subprocess
# every miss), and misses become rare once installation is done.
_resolved_cache: dict[str, str] = {}


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


def _probe(path: str | None, args: list[str], *, timeout: float = 15.0) -> bool:
    if not path:
        return False
    try:
        subprocess.run(
            [path, *args],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=timeout,
            check=True,
        )
        return True
    except subprocess.TimeoutExpired:
        # Binary exists but didn't answer in time — almost always transient
        # CPU/IO contention at container/worker boot (every thread probes at
        # once). Treat as a miss so we re-probe later (the cache is
        # positive-only), but log it distinctly so it isn't mistaken for a
        # genuinely absent binary. 5s was too tight under boot load; 15s
        # still bounds a truly hung binary.
        log.warning("%s probe timed out after %.0fs (will re-probe later)", path, timeout)
        return False
    except (subprocess.SubprocessError, OSError):
        return False


def _cached_resolve(name: str, override: str | None, probe_args: list[str]) -> str | None:
    """Resolve `name` once and remember the hit. Misses re-probe so a
    vendor binary dropped into place after worker boot becomes
    available without a restart."""
    hit = _resolved_cache.get(name)
    if hit:
        return hit
    path = _resolve(name, override)
    if path and _probe(path, probe_args):
        _resolved_cache[name] = path
        log.info("%s: %s", name, path)
        return path
    return None


def exiftool_path() -> str | None:
    s = get_settings()
    return _cached_resolve("exiftool", s.paths.exiftool, ["-ver"])


def ffmpeg_path() -> str | None:
    s = get_settings()
    return _cached_resolve("ffmpeg", s.paths.ffmpeg, ["-version"])

"""Build a web-playable H.264 proxy for a video the browser can't decode.

Triggered lazily: the API enqueues a `transcode_proxy` job the first time
playback of a video fails (HEVC / .mkv / .avi / .3gp / …). We write a
1080p-capped H.264 + AAC MP4 with +faststart under data/proxies/, keyed by
the source sha256 so identical files share one proxy. The original is never
touched (the photo root is mounted read-only anyway).
"""
from __future__ import annotations

import logging
import os
import subprocess
import uuid
from dataclasses import dataclass
from pathlib import Path

from ..external import ffmpeg_path
from ..paths import PROXIES_DIR

log = logging.getLogger(__name__)

# Longest-edge cap. force_original_aspect_ratio=decrease only ever shrinks;
# force_divisible_by=2 keeps dimensions even for yuv420p / libx264.
MAX_W = 1920
MAX_H = 1080
# Generous ceiling so a long clip on a slow NAS CPU isn't killed mid-encode,
# but a wedged ffmpeg can't hang a worker thread forever.
TRANSCODE_TIMEOUT = 7200  # seconds (2h)


def proxy_path(sha256: str) -> Path:
    """data/proxies/ab/cd/<sha>.mp4"""
    return PROXIES_DIR / sha256[:2] / sha256[2:4] / f"{sha256}.mp4"


@dataclass
class ProxyResult:
    status: str           # done | failed
    error: str | None = None


def generate_proxy(src: str, sha256: str) -> ProxyResult:
    """Transcode `src` to an H.264 MP4 proxy at proxy_path(sha256).

    Idempotent: if the proxy already exists we report success without
    re-encoding. Writes to a unique temp file then atomically renames, so a
    concurrent/duplicate run (e.g. a reclaimed job) can't serve a partial
    file — last finisher wins, which is harmless.
    """
    dest = proxy_path(sha256)
    if dest.exists() and dest.stat().st_size > 0:
        return ProxyResult(status="done")

    ff = ffmpeg_path()
    if not ff:
        return ProxyResult(status="failed", error="ffmpeg not available")

    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(f"{sha256}.{uuid.uuid4().hex}.tmp.mp4")
    cmd = [
        ff, "-y", "-hide_banner", "-loglevel", "error",
        "-i", src,
        # Only the first video + (optional) first audio — drops subtitle /
        # data streams that .mkv/.mov carry but MP4 can't hold.
        "-map", "0:v:0", "-map", "0:a:0?",
        "-vf", (f"scale={MAX_W}:{MAX_H}:force_original_aspect_ratio=decrease"
                ":force_divisible_by=2"),
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
        "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "128k",
        "-movflags", "+faststart",
        str(tmp),
    ]
    try:
        proc = subprocess.run(
            cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
            timeout=TRANSCODE_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        _unlink(tmp)
        return ProxyResult(status="failed", error="ffmpeg timed out")
    except OSError as e:
        _unlink(tmp)
        return ProxyResult(status="failed", error=str(e))

    if proc.returncode != 0 or not tmp.exists() or tmp.stat().st_size == 0:
        err = (proc.stderr or b"").decode("utf-8", "replace").strip()[-500:]
        _unlink(tmp)
        return ProxyResult(status="failed", error=err or f"ffmpeg exit {proc.returncode}")

    try:
        os.replace(tmp, dest)   # atomic within the same filesystem
    except OSError as e:
        _unlink(tmp)
        return ProxyResult(status="failed", error=f"rename failed: {e}")
    log.info("video proxy built: %s (%d bytes)", sha256[:12], dest.stat().st_size)
    return ProxyResult(status="done")


def _unlink(p: Path) -> None:
    try:
        p.unlink()
    except OSError:
        pass


def enforce_cache_cap(max_bytes: int) -> int:
    """Evict least-recently-played proxies until data/proxies fits `max_bytes`.

    LRU = oldest mtime; each serve bumps the proxy's mtime (see the /video
    route), so the survivors are the ones actually watched recently. An
    evicted proxy simply regenerates on next view. max_bytes <= 0 disables.
    Returns the number of files deleted.
    """
    if max_bytes <= 0:
        return 0
    entries: list[tuple[float, int, Path]] = []
    total = 0
    for p in PROXIES_DIR.rglob("*.mp4"):
        try:
            st = p.stat()
        except OSError:
            continue
        entries.append((st.st_mtime, st.st_size, p))
        total += st.st_size
    if total <= max_bytes:
        return 0
    entries.sort(key=lambda e: e[0])   # oldest mtime first
    deleted = 0
    for _mtime, size, p in entries:
        if total <= max_bytes:
            break
        try:
            p.unlink()
            total -= size
            deleted += 1
        except OSError:
            pass
    if deleted:
        log.info("proxy cache: evicted %d file(s) to stay under %d bytes",
                 deleted, max_bytes)
    return deleted

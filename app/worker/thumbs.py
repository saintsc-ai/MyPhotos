"""Thumbnail generation.

Thumbs are addressed by photo SHA-256, so:
  data/thumbs/<size>/ab/cd/abcd...jpg

This means two photos that happen to be identical (e.g. same image in
two folders) share a single thumbnail file on disk.

For images we use Pillow with optional pillow-heif registration.
For videos we shell out to ffmpeg if available.
"""

from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageOps

from ..config import get_settings
from ..external import ffmpeg_path
from ..paths import THUMBS_DIR

log = logging.getLogger(__name__)


# Optional HEIC support — register if pillow-heif is installed.
try:
    from pillow_heif import register_heif_opener  # type: ignore

    register_heif_opener()
    _HEIC_OK = True
except ImportError:  # pragma: no cover
    _HEIC_OK = False


@dataclass
class ThumbResult:
    sizes_written: list[int]
    status: str  # ok | partial | failed
    error: str | None = None


def thumb_path(sha256: str, size: int) -> Path:
    """data/thumbs/<size>/ab/cd/<sha>.jpg"""
    return THUMBS_DIR / str(size) / sha256[:2] / sha256[2:4] / f"{sha256}.jpg"


def _save_image_thumbs(src: str, sha256: str, sizes: list[int], quality: int) -> list[int]:
    written: list[int] = []
    with Image.open(src) as im:
        im = ImageOps.exif_transpose(im)  # apply orientation
        if im.mode not in ("RGB", "L"):
            im = im.convert("RGB")
        for size in sizes:
            out = thumb_path(sha256, size)
            if out.exists():
                written.append(size)
                continue
            out.parent.mkdir(parents=True, exist_ok=True)
            copy = im.copy()
            copy.thumbnail((size, size), Image.Resampling.LANCZOS)
            copy.save(out, format="JPEG", quality=quality, optimize=True)
            written.append(size)
    return written


def _save_video_thumbs(src: str, sha256: str, sizes: list[int], quality: int) -> list[int]:
    ff = ffmpeg_path()
    if not ff:
        raise RuntimeError("ffmpeg not available")
    largest = max(sizes)
    tmp = THUMBS_DIR / "tmp"
    tmp.mkdir(parents=True, exist_ok=True)
    extract = tmp / f"{sha256}.jpg"
    subprocess.run(
        [
            ff,
            "-y",
            "-ss",
            "00:00:01",
            "-i",
            src,
            "-frames:v",
            "1",
            "-vf",
            f"scale='min({largest},iw)':-2",
            "-q:v",
            "3",
            str(extract),
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=60,
        check=True,
    )
    try:
        written = _save_image_thumbs(str(extract), sha256, sizes, quality)
    finally:
        try:
            extract.unlink()
        except OSError:
            pass
    return written


def generate(src: str, sha256: str, *, media_kind: str) -> ThumbResult:
    s = get_settings()
    sizes = sorted(set(s.thumbnails.sizes))
    quality = s.thumbnails.quality
    try:
        if media_kind == "video":
            written = _save_video_thumbs(src, sha256, sizes, quality)
        else:
            written = _save_image_thumbs(src, sha256, sizes, quality)
    except Exception as e:
        return ThumbResult(sizes_written=[], status="failed", error=str(e)[:1000])

    status = "ok" if len(written) == len(sizes) else ("partial" if written else "failed")
    return ThumbResult(sizes_written=written, status=status)

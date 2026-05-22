"""Thumbnail generation.

Thumbs are addressed by photo SHA-256, so:
  data/thumbs/<size>/ab/cd/abcd...jpg

This means two photos that happen to be identical (e.g. same image in
two folders) share a single thumbnail file on disk.

Pipeline by file kind:
  - image (jpeg/png/...) → Pillow direct
  - image (HEIC/HEIF)    → Pillow with pillow-heif registered
  - image (RAW)          → ExifTool extracts the embedded JPEG preview,
                            then Pillow scales it down. Decoding the raw
                            sensor data with libraw would be far slower.
  - video                → ffmpeg pulls one frame, then the image path.
"""

from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageOps

from ..config import get_settings
from ..external import exiftool_path, ffmpeg_path
from ..paths import THUMBS_DIR


# Extensions for which we always go through ExifTool preview extraction
# instead of trying Pillow first. Includes DNG because Pillow's DNG
# support is unreliable.
RAW_EXTS = {
    "raw", "rw2", "arw", "cr2", "cr3", "nef", "orf", "pef", "dng",
    "raf", "srw", "rwl", "iiq",
}


def is_raw_path(path: str) -> bool:
    p = path.lower()
    return "." in p and p.rsplit(".", 1)[1] in RAW_EXTS

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


def _extract_raw_preview(src: str, dest: Path) -> None:
    """Pull the largest embedded JPEG out of a RAW file via ExifTool.

    Tries tags in descending size order. Raises if none yield bytes.
    """
    tool = exiftool_path()
    if not tool:
        raise RuntimeError("exiftool not available (required for RAW preview)")

    # ExifTool tag names that commonly hold an embedded JPEG, large to small.
    for tag in ("-JpgFromRaw", "-PreviewImage", "-OtherImage", "-ThumbnailImage"):
        try:
            proc = subprocess.run(
                [tool, "-b", tag, src],
                capture_output=True,
                timeout=20,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as e:
            raise RuntimeError(f"exiftool invocation failed: {e}")
        # ExifTool returns 0 with empty stdout when the tag is absent.
        if proc.returncode == 0 and proc.stdout and len(proc.stdout) > 1024:
            dest.write_bytes(proc.stdout)
            return
    raise RuntimeError("no usable embedded preview in RAW file")


def _save_raw_thumbs(src: str, sha256: str, sizes: list[int], quality: int) -> list[int]:
    tmp = THUMBS_DIR / "tmp"
    tmp.mkdir(parents=True, exist_ok=True)
    preview = tmp / f"{sha256}.preview.jpg"
    _extract_raw_preview(src, preview)
    try:
        return _save_image_thumbs(str(preview), sha256, sizes, quality)
    finally:
        try:
            preview.unlink()
        except OSError:
            pass


def _save_video_thumbs(src: str, sha256: str, sizes: list[int], quality: int) -> list[int]:
    ff = ffmpeg_path()
    if not ff:
        raise RuntimeError("ffmpeg not available")
    largest = max(sizes)
    tmp = THUMBS_DIR / "tmp"
    tmp.mkdir(parents=True, exist_ok=True)
    extract = tmp / f"{sha256}.jpg"

    # iPhone Live Photo .MOV files are often <1s, so a fixed 1s seek lands
    # past the end and ffmpeg silently writes no output. Try a few seek
    # timestamps in descending order — first one that produces a non-empty
    # JPEG wins. The "best" frame is mid-clip; 0 is the last-resort fallback.
    last_err: Exception | None = None
    for seek in ("00:00:01", "00:00:00.5", "00:00:00"):
        try:
            extract.unlink()
        except OSError:
            pass
        try:
            subprocess.run(
                [
                    ff,
                    "-y",
                    "-ss",
                    seek,
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
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as e:
            last_err = e
            continue
        if extract.exists() and extract.stat().st_size > 1024:
            break
    else:
        msg = "ffmpeg produced no frame at 1s / 0.5s / 0s"
        if last_err:
            msg += f" (last: {last_err})"
        raise RuntimeError(msg)

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
        elif is_raw_path(src):
            written = _save_raw_thumbs(src, sha256, sizes, quality)
        else:
            written = _save_image_thumbs(src, sha256, sizes, quality)
    except Exception as e:
        return ThumbResult(sizes_written=[], status="failed", error=str(e)[:1000])

    status = "ok" if len(written) == len(sizes) else ("partial" if written else "failed")
    return ThumbResult(sizes_written=written, status=status)

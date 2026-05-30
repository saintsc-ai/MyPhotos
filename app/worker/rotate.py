"""Pure-IO helpers for lossless photo rotation.

Extracted from app.api.routes_photos.bulk_rotate. The route handler
still owns the ACL checks, the DB updates and the audit-log writes;
this module owns the SYSTEM-level pieces:

  - the EXIF Orientation transition tables (which new value follows
    a given direction from a given current value)
  - the ExifTool subprocess invocation (with retry + ignore-minor-
    errors handling we learned the hard way)
  - the file re-hash (sha256 + content_signature) after the tag write
  - the Pillow-driven thumbnail regeneration at the new sha path

Pulling these out makes them unit-testable without spinning up a
FastAPI app, and lets the route focus on orchestration. None of the
functions here touch the database — they take an absolute path /
sha string and return a result struct.
"""

from __future__ import annotations

import hashlib
import logging
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)


# EXIF Orientation transition tables. Cover all 8 starting values
# (1..8) including the mirrored variants (2/4/5/7) so a rotation
# applied to an already-mirrored photo stays consistent. Per the
# EXIF 2.32 spec:
#   1=normal  2=mirror-h  3=180  4=mirror-v
#   5=mirror-h+270  6=90 CW  7=mirror-h+90  8=270 CW (= 90 CCW)
_ROTATE_CW  = {1: 6, 2: 7, 3: 8, 4: 5, 5: 2, 6: 3, 7: 4, 8: 1}
_ROTATE_CCW = {1: 8, 2: 5, 3: 6, 4: 7, 5: 4, 6: 1, 7: 2, 8: 3}
_ROTATE_180 = {1: 3, 2: 4, 3: 1, 4: 2, 5: 7, 6: 8, 7: 5, 8: 6}


def next_orientation(current: int | None, direction: str) -> int:
    """Look up the new EXIF Orientation that follows `direction` from
    `current`. Unknown / out-of-spec current values are treated as 1
    (the upright default) so an unrecognised metadata never blocks
    a rotation."""
    table = (_ROTATE_CW if direction == "cw"
             else _ROTATE_CCW if direction == "ccw"
             else _ROTATE_180)
    cur = current if current in table else 1
    return table[cur]


@dataclass
class ExifWriteResult:
    """Outcome of the ExifTool subprocess. `error` is the trimmed
    stderr (≤200 chars) suitable for surfacing to the user."""
    ok: bool
    error: str = ""


def rotate_orientation_tag(
    tool: str,
    abs_path: str,
    new_orient: int,
) -> ExifWriteResult:
    """Rewrite the EXIF Orientation tag on the file at `abs_path` to
    `new_orient`, using the ExifTool binary at `tool`.

    Implements two write-strategy fallbacks:

    1. `-overwrite_original` is the fast path. ExifTool writes the
       new file to `<file>_exiftool_tmp` next to the original then
       atomically renames. Fails on directories that block new-file
       creation (Synology share ACL, some NFS mounts).
    2. `-overwrite_original_in_place` rewrites the file in place
       without a temp companion — slower but works as long as the
       file itself is writable. Triggered only when the first path
       fails with a `_exiftool_tmp` stderr.

    `-m` (ignore minor errors) is always passed: real-world files
    often carry [minor] complaints (e.g. "Bad MakerNotes offset for
    NikonCaptureVersion" on resaved Nikon JPEGs) that ExifTool would
    otherwise treat as fatal. Major errors (truncated file, unreadable
    format) still fail.

    `#=` forces numeric write so "6" stays as int 6 instead of being
    parsed as the human-readable string name.
    """
    def _run(extra_flag: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [tool, "-m", extra_flag,
             f"-Orientation#={new_orient}",
             str(abs_path)],
            capture_output=True,
            timeout=30,
            check=False,
        )

    try:
        proc = _run("-overwrite_original")
        err = (proc.stderr or b"").decode("utf-8", errors="replace").strip()
        if proc.returncode != 0 and "_exiftool_tmp" in err:
            log.warning(
                "rotate: %s tmp-file create failed, retrying "
                "with -overwrite_original_in_place", abs_path,
            )
            proc = _run("-overwrite_original_in_place")
            err = (proc.stderr or b"").decode("utf-8", errors="replace").strip()
    except (OSError, subprocess.SubprocessError) as e:
        log.exception("rotate: exiftool launch failed for %s", abs_path)
        return ExifWriteResult(ok=False, error=f"exiftool launch: {e}")

    if proc.returncode != 0:
        log.warning(
            "rotate: %s exiftool exit %d stderr=%r",
            abs_path, proc.returncode, err[:500],
        )
        reason = err[:200] or f"exiftool exit {proc.returncode}"
        return ExifWriteResult(ok=False, error=reason)

    out = (proc.stdout or b"").decode("utf-8", errors="replace").strip()
    log.warning("rotate: %s exiftool ok stdout=%r", abs_path, out[:200])
    return ExifWriteResult(ok=True)


@dataclass
class RehashResult:
    sha256: str
    file_size: int
    content_signature: str


def rehash_file(abs_path: str) -> RehashResult:
    """Stream-hash the file at `abs_path` and return its sha256,
    on-disk size, and the `size:mtime_ns` content signature the
    scanner uses to detect changes.

    Raises OSError on read failure — caller decides how to surface it.
    """
    h = hashlib.sha256()
    with open(abs_path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    st = os.stat(abs_path)
    return RehashResult(
        sha256=h.hexdigest(),
        file_size=st.st_size,
        content_signature=f"{st.st_size}:{st.st_mtime_ns}",
    )


@dataclass
class ThumbRegenResult:
    """How the thumbnail regen went.

    `status` is one of 'ok' / 'partial' / 'pending' to mirror the
    Photo.thumb_status enum the caller writes to the DB row.
    `written_sizes` is the list of sizes successfully saved at the
    new sha path (empty when status='pending', meaning we deferred
    to the worker's full generate()).
    """
    status: str
    written_sizes: list[int]


def regenerate_rotated_thumbnails(
    old_sha: str | None,
    new_sha: str,
    direction: str,
    sizes: list[int],
    quality: int,
) -> ThumbRegenResult:
    """Pixel-rotate the existing thumbnail and save it at the new
    sha-keyed paths.

    Why this instead of re-running worker.thumbs.generate(): for HEIC,
    pillow_heif normalises the irot atom during decode, so an
    EXIF-only orientation change has ZERO visible effect on a
    regenerated thumb. Grabbing the user-visible OLD thumb and
    rotating ITS pixels by the requested direction works for every
    format and is sub-100 ms.

    Returns status='pending' (without writing anything) when no prior
    thumb exists on disk — caller should fall back to the full
    worker generate() in that case.
    """
    from PIL import Image as _PIL_Image

    from .thumbs import thumb_path as _thumb_path

    sizes_sorted = sorted(set(sizes))

    # Pick the largest existing thumb at the OLD sha. That's what the
    # user has been looking at; rotating it gives a visually-correct
    # result regardless of how the source file's EXIF was interpreted.
    src_thumb: Path | None = None
    for sz in reversed(sizes_sorted):
        cand = _thumb_path(old_sha, sz) if old_sha else None
        if cand and cand.exists():
            src_thumb = cand
            break

    if src_thumb is None:
        return ThumbRegenResult(status="pending", written_sizes=[])

    written: list[int] = []
    with _PIL_Image.open(src_thumb) as im:
        im.load()
        if im.mode not in ("RGB", "L"):
            im = im.convert("RGB")
        # Pillow rotate is mathematically CCW so we negate for CW.
        # expand=True keeps the full image after a 90° rotation
        # (otherwise it'd be cropped to the original frame).
        angle = (90 if direction == "ccw"
                 else -90 if direction == "cw"
                 else 180)
        rotated_full = im.rotate(angle, expand=True)
        for size in sizes_sorted:
            out_path = _thumb_path(new_sha, size)
            out_path.parent.mkdir(parents=True, exist_ok=True)
            copy = rotated_full.copy()
            copy.thumbnail((size, size), _PIL_Image.Resampling.LANCZOS)
            copy.save(out_path, format="JPEG", quality=quality, optimize=True)
            written.append(size)

    status = "ok" if len(written) == len(sizes_sorted) else "partial"
    return ThumbRegenResult(status=status, written_sizes=written)

"""Bulk-download zip building, extracted from app.api.routes_photos.

The route handler still owns token issuance + ACL + the FileResponse
streaming; this module owns the filesystem-facing bits:

  - sweep_old_downloads(): garbage-collect stale zips in data/tmp/
  - unique_arc_name():     pick a non-colliding name for the archive
  - build_photo_zip():     stream a list of files into a zip on disk

Pulling these out makes them unit-testable (no FastAPI required) and
gives the route a single named call instead of an inline 50-line
try/except block.
"""

from __future__ import annotations

import logging
import time
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)


def sweep_old_downloads(tmp_dir: Path, max_age_seconds: int = 3600) -> None:
    """Best-effort cleanup of stale bulk-download zips left over from
    failed / cancelled downloads. Called from prepare; cheap on small
    sets. Errors are swallowed — the sweep is opportunistic and a
    locked / in-use file should not block a new download."""
    now = time.time()
    try:
        for f in tmp_dir.glob("download_*.zip"):
            try:
                if now - f.stat().st_mtime > max_age_seconds:
                    f.unlink()
            except OSError:
                pass
    except OSError:
        pass


def unique_arc_name(taken: set[str], name: str) -> str:
    """Pick a name not already in `taken`, appending `_2` / `_3` /
    etc. before the extension if needed. Mutates `taken` in place
    so successive calls cooperate.

    Used to dedupe archive entries when two source files share a
    filename (e.g. 'IMG_0001.jpg' in two different folders both
    selected for the same zip)."""
    if name not in taken:
        taken.add(name)
        return name
    if "." in name:
        base, _, ext = name.rpartition(".")
        ext = "." + ext
    else:
        base, ext = name, ""
    i = 2
    while True:
        candidate = f"{base}_{i}{ext}"
        if candidate not in taken:
            taken.add(candidate)
            return candidate
        i += 1


@dataclass
class ZipItem:
    """One photo to put in the zip. `photo_id` is the original DB id,
    propagated into the skipped[] list so the caller can map back to
    photos that failed."""
    photo_id: int
    src_path: Path
    suggested_name: str


@dataclass
class ZipBuildResult:
    added: int = 0
    # photo_ids that couldn't be added (missing file, OS error, etc.)
    skipped: list[int] = field(default_factory=list)


def build_photo_zip(items: list[ZipItem], dest: Path) -> ZipBuildResult:
    """Bundle every item into a zip at `dest`. Returns counts.

    ZIP_STORED — JPEGs/PNGs/HEICs/RAWs are already compressed; spending
    CPU on DEFLATE for them is waste. Just bundle. Caller is responsible
    for picking the dest path and cleaning it up if the result has
    added==0 (decide whether to surface "nothing to download" or an
    error).

    Raises on a catastrophic zipfile failure (disk full, unwritable
    dest) — caller decides whether to unlink the partial file and
    return an error. Per-item OSError is captured into skipped[].
    """
    arc_names: set[str] = set()
    result = ZipBuildResult()

    with zipfile.ZipFile(dest, "w", zipfile.ZIP_STORED, allowZip64=True) as zf:
        for item in items:
            if not item.src_path.exists():
                result.skipped.append(item.photo_id)
                continue
            arcname = unique_arc_name(
                arc_names, item.suggested_name or f"photo_{item.photo_id}",
            )
            try:
                zf.write(item.src_path, arcname=arcname)
            except OSError as e:
                log.warning("zip add failed for photo %s: %s", item.photo_id, e)
                result.skipped.append(item.photo_id)
                continue
            result.added += 1
    return result

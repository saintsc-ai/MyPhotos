"""First-pass discovery — walk a root and register photos by path only.

Memory-safe walk: uses os.scandir iteratively and processes one directory
at a time. The "미분류" folder with ~73k entries was the motivating case.

For each new file we INSERT a stub Photo (sha256/exif/thumb pending) and
enqueue an `index_file` job. The worker dispatcher picks those up and
fills in hash + EXIF + thumbnails.

Idempotent: re-running on a root only enqueues files whose
(size, mtime_ns) signature changed.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime
from typing import Iterator

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Photo, Root
from .utils import classify, filter_dir_entries, nfc, to_posix_rel
from ..worker.jobs import enqueue

log = logging.getLogger(__name__)


def _walk(root_abs: str) -> Iterator["os.DirEntry"]:
    """Yield file DirEntry objects under root_abs, lazily.

    Uses an explicit stack instead of recursion or os.walk so we don't
    materialize a giant list. filter_dir_entries handles ignore rules.
    """
    stack: list[str] = [root_abs]
    while stack:
        d = stack.pop()
        try:
            with os.scandir(d) as it:
                subdirs, files = filter_dir_entries(it)
        except OSError as e:
            log.warning("scandir failed for %s: %s", d, e)
            continue
        # Push subdirs first so files in current dir are yielded before recursion
        for f in files:
            yield f
        for sd in subdirs:
            stack.append(sd.path)


def _signature(size: int, mtime_ns: int) -> str:
    return f"{size}:{mtime_ns}"


# iPhone Live Photo extensions only — HEIC/HEIF (iOS 11+ default) and
# JPG/JPEG (older devices or "Most Compatible" setting) paired with MOV.
# Restricting to these specific pairs avoids false-positives like
# PNG+MP4 or user-created same-stem image+video pairs that aren't
# actually a Live Photo.
_LIVE_STILL_EXTS = {"heic", "heif", "jpg", "jpeg"}
_LIVE_VIDEO_EXTS = {"mov"}


def _is_live_pair_ext(still_ext: str, video_ext: str) -> bool:
    s = (still_ext or "").lower().lstrip(".")
    v = (video_ext or "").lower().lstrip(".")
    return s in _LIVE_STILL_EXTS and v in _LIVE_VIDEO_EXTS


def _try_pair_companion(db: Session, *, root_id: int, photo: Photo) -> None:
    """Look for a same-stem opposite-kind sibling and pair via companion_id.

    Cheap query — uses the existing (root_id, rel_path) unique index for
    the LIKE 'parent/stem.%' prefix lookup. Bidirectional so either side
    of the pair can find the other. Limited to iPhone Live Photo
    extensions (see _LIVE_STILL_EXTS / _LIVE_VIDEO_EXTS).
    """
    name = photo.filename or ""
    if "." not in name:
        return
    stem = name.rsplit(".", 1)[0]
    if not stem:
        return
    # Cheap ext gate — bail before hitting the DB if this file can't
    # possibly be one half of a Live Photo pair.
    photo_ext = (photo.ext or "").lower().lstrip(".")
    if photo.media_kind == "image":
        if photo_ext not in _LIVE_STILL_EXTS:
            return
    else:
        if photo_ext not in _LIVE_VIDEO_EXTS:
            return
    parent = photo.rel_path.rsplit("/", 1)[0] if "/" in photo.rel_path else ""
    pattern = (parent + "/" if parent else "") + stem + ".%"
    opposite = "video" if photo.media_kind == "image" else "image"
    sibling = db.execute(
        select(Photo).where(
            Photo.root_id == root_id,
            Photo.media_kind == opposite,
            Photo.rel_path.like(pattern),
            Photo.companion_id.is_(None),
            Photo.id != photo.id,
        )
    ).scalar_one_or_none()
    if sibling is None:
        return
    # Same-folder guard — SQL LIKE's % matches any character including
    # '/', so 'parent/stem.%' could in theory match
    # 'parent/stem.weird/sub.HEIC' in a subfolder. Verify the sibling's
    # parent directory matches exactly.
    sib_parent = sibling.rel_path.rsplit("/", 1)[0] if "/" in sibling.rel_path else ""
    if sib_parent != parent:
        return
    # Sibling's extension also has to fit the Live Photo shape — guards
    # against e.g. an HEIC paired with a stray .mp4 of the same stem.
    if photo.media_kind == "image":
        if not _is_live_pair_ext(photo_ext, sibling.ext):
            return
    else:
        if not _is_live_pair_ext(sibling.ext, photo_ext):
            return
    # Same-day sanity check — Live Photo pairs are written within
    # seconds. Pairing files years apart that happen to share a stem
    # (e.g. user re-used "vacation.mp4") would be wrong.
    if photo.mtime and sibling.mtime:
        delta = abs((photo.mtime - sibling.mtime).total_seconds())
        if delta > 86400:                       # > 24 h apart
            return
    photo.companion_id = sibling.id
    sibling.companion_id = photo.id


def discover_root(db: Session, root: Root, *, limit: int | None = None) -> dict[str, int]:
    """Walk a root, upsert Photo rows, enqueue index_file jobs for new/changed files.

    Returns a small dict of counters.
    """
    counters = {"seen": 0, "added": 0, "changed": 0, "skipped": 0, "enqueued": 0}
    root_abs = root.abs_path

    for entry in _walk(root_abs):
        counters["seen"] += 1
        if limit and counters["seen"] >= limit:
            break

        name = nfc(entry.name)
        try:
            st = entry.stat(follow_symlinks=False)
        except OSError:
            counters["skipped"] += 1
            continue

        kind, ext = classify(name)
        if kind is None:
            counters["skipped"] += 1
            continue

        rel_path = to_posix_rel(entry.path, root_abs)
        sig = _signature(st.st_size, st.st_mtime_ns)

        existing = db.execute(
            select(Photo).where(Photo.root_id == root.id, Photo.rel_path == rel_path)
        ).scalar_one_or_none()

        if existing is None:
            photo = Photo(
                root_id=root.id,
                rel_path=rel_path,
                filename=name,
                ext=ext,
                media_kind=kind,
                file_size=st.st_size,
                mtime=datetime.fromtimestamp(st.st_mtime),
                content_signature=sig,
            )
            db.add(photo)
            db.flush()
            # Live Photo / HEIC↔MOV pairing: an iPhone Live Photo lands
            # as IMG_1234.HEIC + IMG_1234.MOV in the same folder. We
            # link them bidirectionally via companion_id so the UI can
            # treat the still as the primary view and play the MOV on
            # demand. Match by:
            #   - same root + same parent directory + same stem
            #   - opposite media_kind (image ↔ video)
            #   - both files created within ~1 day of each other so we
            #     don't pair coincidentally-named files years apart
            #   - neither side already paired
            _try_pair_companion(db, root_id=root.id, photo=photo)
            enqueue(db, kind="index_file", payload={"photo_id": photo.id})
            counters["added"] += 1
            counters["enqueued"] += 1
        elif existing.content_signature != sig:
            # Re-process — reset stage status
            existing.file_size = st.st_size
            existing.mtime = datetime.fromtimestamp(st.st_mtime)
            existing.content_signature = sig
            existing.exif_status = "pending"
            existing.thumb_status = "pending"
            existing.status = "active"
            enqueue(db, kind="index_file", payload={"photo_id": existing.id})
            counters["changed"] += 1
            counters["enqueued"] += 1
        else:
            counters["skipped"] += 1

        # Commit in batches so a long scan stays incremental
        if counters["seen"] % 500 == 0:
            db.commit()

    db.commit()
    root.last_full_scan = datetime.utcnow()
    db.commit()
    log.info("discover_root[%s] counters: %s", root.label, counters)
    return counters

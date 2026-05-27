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

from ..models import Photo, Root, UploadPending
from .utils import (
    classify, filter_dir_entries, nfc, rel_path_is_ignored,
    root_ignore_paths, to_posix_rel,
)
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


def apply_ignore_sweep(db: Session, root: Root) -> dict[str, int]:
    """Reconcile photos.status against root.ignore_paths without
    touching the filesystem.

    Two directions:
      1. status='active'  + rel_path matches an ignore prefix
         → status='ignored'   (preserves ratings/tags/comments, hides
         from gallery / search / stats)
      2. status='ignored' + rel_path no longer matches any prefix
         → status='active'    (auto-restore when an entry is removed)

    Cheap — pure SQL UPDATE, no walk. Called by the PATCH /admin/roots
    endpoint so ignore-list edits show up in the gallery immediately,
    and also by discover_root so a full scan keeps things consistent.
    """
    from sqlalchemy import or_, update
    counters: dict[str, int] = {}
    ignore_paths = root_ignore_paths(root)

    if ignore_paths:
        like_clauses = []
        for ip in ignore_paths:
            like_clauses.append(Photo.rel_path == ip)
            like_clauses.append(Photo.rel_path.like(ip + "/%"))
        res = db.execute(
            update(Photo)
            .where(
                Photo.root_id == root.id,
                Photo.status == "active",
                or_(*like_clauses),
            )
            .values(status="ignored")
        )
        if res.rowcount:
            counters["ignored_added"] = res.rowcount
        res = db.execute(
            update(Photo)
            .where(
                Photo.root_id == root.id,
                Photo.status == "ignored",
                ~or_(*like_clauses),
            )
            .values(status="active")
        )
        if res.rowcount:
            counters["ignored_restored"] = res.rowcount
    else:
        # Empty list → restore everything that was ignored.
        res = db.execute(
            update(Photo)
            .where(Photo.root_id == root.id, Photo.status == "ignored")
            .values(status="active")
        )
        if res.rowcount:
            counters["ignored_restored"] = res.rowcount
    db.commit()
    return counters


def discover_root(db: Session, root: Root, *, limit: int | None = None) -> dict[str, int]:
    """Walk a root, upsert Photo rows, enqueue index_file jobs for new/changed files.

    For *full* scans (limit not set) this also does reconciliation:
    photos whose underlying file disappeared from the filesystem since
    the previous scan are flagged `status='missing'` so they drop out of
    the gallery / map / search without losing their DB row (ratings,
    comments, tags survive). A subsequent scan that finds the file again
    at the same path resurrects the row to 'active'.

    Limit scans skip reconciliation — a sample scan should never decide
    files it didn't visit are gone.

    Returns a small dict of counters.
    """
    counters = {
        "seen": 0, "added": 0, "changed": 0, "skipped": 0, "enqueued": 0,
        "missing": 0, "resurrected": 0,
    }
    root_abs = root.abs_path

    # Snapshot the active set up-front so we can compute "active before
    # the scan but never seen during the walk" = disappeared files.
    do_reconcile = limit is None
    pre_active_ids: set[int] = set()
    if do_reconcile:
        pre_active_ids = {
            rid for (rid,) in db.execute(
                select(Photo.id).where(
                    Photo.root_id == root.id,
                    Photo.status == "active",
                )
            ).all()
        }
    seen_ids: set[int] = set()
    # Newly inserted photo ids buffered per-batch so FTS can re-index
    # them at the same cadence as the SQLite commits. Without this, a
    # fresh photo doesn't show up in search until something else
    # triggers a rebuild for that id.
    fts_pending: list[int] = []
    ignore_paths = root_ignore_paths(root)

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
        # User-managed ignore paths — skip the file entirely. Existing
        # rows that land under an ignore path get reconciled to
        # status='ignored' in the sweep below, so user-applied
        # ratings/tags/comments survive.
        if rel_path_is_ignored(rel_path, ignore_paths):
            counters["skipped"] += 1
            continue
        sig = _signature(st.st_size, st.st_mtime_ns)

        existing = db.execute(
            select(Photo).where(Photo.root_id == root.id, Photo.rel_path == rel_path)
        ).scalar_one_or_none()

        if existing is None:
            # Was this path uploaded via the API? Pick up the uploader
            # to populate Photo.owner_user_id, then drop the pending row.
            pending = db.execute(
                select(UploadPending).where(
                    UploadPending.root_id == root.id,
                    UploadPending.rel_path == rel_path,
                )
            ).scalar_one_or_none()
            owner_user_id = pending.user_id if pending is not None else None
            photo = Photo(
                root_id=root.id,
                rel_path=rel_path,
                filename=name,
                ext=ext,
                media_kind=kind,
                file_size=st.st_size,
                mtime=datetime.fromtimestamp(st.st_mtime),
                content_signature=sig,
                owner_user_id=owner_user_id,
            )
            db.add(photo)
            db.flush()
            if pending is not None:
                db.delete(pending)
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
            seen_ids.add(photo.id)
            fts_pending.append(photo.id)
        elif existing.content_signature != sig:
            # Re-process — reset stage status
            existing.file_size = st.st_size
            existing.mtime = datetime.fromtimestamp(st.st_mtime)
            existing.content_signature = sig
            existing.exif_status = "pending"
            existing.thumb_status = "pending"
            # Resurrect rows that had been flagged missing earlier and
            # now exist again at the same path.
            if existing.status == "missing":
                counters["resurrected"] += 1
            existing.status = "active"
            enqueue(db, kind="index_file", payload={"photo_id": existing.id})
            counters["changed"] += 1
            counters["enqueued"] += 1
            seen_ids.add(existing.id)
        else:
            # Same path, same signature — also a resurrection case if it
            # was marked missing before (file restored from backup).
            if existing.status == "missing":
                existing.status = "active"
                counters["resurrected"] += 1
            counters["skipped"] += 1
            seen_ids.add(existing.id)

        # Commit in batches so a long scan stays incremental — shorter
        # batches keep the SQLite writer lock from being held for
        # multiple seconds in a row, which would block API writes
        # (set_rating, set_photo_tags) and the ML worker's
        # classify_embedding inserts. 200 is small enough that
        # contention is rare on typical libraries while still keeping
        # per-photo commit overhead reasonable.
        if counters["seen"] % 200 == 0:
            db.commit()
            if fts_pending:
                from .. import fts as _fts
                _fts.bulk_rebuild(db, fts_pending)
                db.commit()
                fts_pending.clear()

    db.commit()
    if fts_pending:
        from .. import fts as _fts
        _fts.bulk_rebuild(db, fts_pending)
        db.commit()
        fts_pending.clear()

    # Reconciliation: anything that was active before the walk and
    # didn't show up during it has disappeared from the filesystem.
    # Flag in batches so the UPDATE doesn't hold the writer lock for
    # one giant statement on huge libraries.
    if do_reconcile:
        from sqlalchemy import update
        missing_ids = list(pre_active_ids - seen_ids)
        if missing_ids:
            BATCH = 500
            for off in range(0, len(missing_ids), BATCH):
                slice_ = missing_ids[off:off + BATCH]
                db.execute(
                    update(Photo)
                    .where(Photo.id.in_(slice_))
                    .values(status="missing")
                )
                db.commit()
            counters["missing"] = len(missing_ids)

    # Ignore-path sweep (file-system-free) — see apply_ignore_sweep
    # below. Called both here and directly from the PATCH endpoint so
    # an ignore-list change is reflected in the gallery without
    # waiting for a full rescan to finish.
    sweep_counters = apply_ignore_sweep(db, root)
    if sweep_counters.get("ignored_added"):
        counters["ignored_added"] = sweep_counters["ignored_added"]
    if sweep_counters.get("ignored_restored"):
        counters["ignored_restored"] = sweep_counters["ignored_restored"]

    root.last_full_scan = datetime.utcnow()
    db.commit()
    log.info("discover_root[%s] counters: %s", root.label, counters)
    return counters

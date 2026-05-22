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

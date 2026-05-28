"""Dedup auto-cleanup worker handler.

Walks every page-1 of the duplicate-group listing, collecting "keep
the first, trash the rest" candidates, and ships them through the
shared trash service. Progress lands on jobs.progress_done /
jobs.progress_total so the admin UI can show a live bar.

This is the server-side version of what admin.html used to do in a
client loop — moving it here means closing the tab no longer stops
work, and reopening the page shows the live count.
"""

from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from ..admin.routes_duplicates import _dup_subquery
from ..api.routes_photos import trash_photos_core
from ..db import SessionLocal
from ..models import Photo, User
from . import jobs as jobs_mod

log = logging.getLogger(__name__)


# Same constants as the old client loop — keeps per-chunk DB work bounded
# and well under the bulk-delete _BULK_LIMIT (1000).
PAGE_SIZE = 100   # duplicate groups fetched per iteration
CHUNK = 800       # photo ids passed to one trash_photos_core call


def _compute_total(db: Session) -> int:
    """Total photos that would be trashed = sum across all groups of
    (count - 1). Used to seed progress_total before the loop starts."""
    from sqlalchemy import func, select
    subq = _dup_subquery().subquery()
    row = db.execute(
        select(func.coalesce(func.sum(subq.c.n - 1), 0))
    ).scalar_one()
    return int(row or 0)


def _next_chunk_ids(db: Session, page_size: int) -> tuple[list[int], int]:
    """Return (photo_ids_to_trash, group_count) for the current page-1
    snapshot of duplicate groups. Mirrors the client's old "keep the
    earliest, trash the rest" rule (taken_at → mtime → root.label →
    rel_path), restricted to groups still in the dedup view.

    Sort matches the /groups endpoint (recent shots first, then same-
    folder / larger / sha) so the right-rail minimap shrinks
    predictably from the top as the worker chews through groups.
    """
    from sqlalchemy import asc, desc, select
    subq = _dup_subquery().subquery()
    shas = [
        r[0] for r in db.execute(
            select(subq.c.sha256)
            .order_by(
                subq.c.max_taken_at.desc().nullslast(),
                asc(subq.c.dir_variants),
                desc(subq.c.file_size),
                subq.c.sha256,
            )
            .limit(page_size)
        ).all()
    ]
    if not shas:
        return [], 0

    # Walk members in the same order routes_duplicates uses so the
    # "first" item (= keep) matches what the admin saw in the UI.
    rows = db.execute(
        select(Photo.id, Photo.sha256)
        .where(
            Photo.sha256.in_(shas),
            Photo.status == "active",
            Photo.thumb_status.in_(("ok", "partial")),
        )
        .order_by(
            Photo.sha256,
            Photo.taken_at.asc().nullslast(),
            Photo.mtime.asc().nullslast(),
            Photo.root_id,
            Photo.rel_path,
        )
    ).all()
    by_sha: dict[str, list[int]] = {}
    for pid, sha in rows:
        by_sha.setdefault(sha, []).append(pid)

    ids: list[int] = []
    for sha in by_sha:
        ids.extend(by_sha[sha][1:])  # keep [0], trash rest
    return ids, len(by_sha)


def run(db: Session, payload: dict) -> None:
    """Worker entry point. Dispatcher passes us _job_id in payload."""
    job_id = int(payload["_job_id"])
    user_id = int(payload["user_id"])

    user = db.get(User, user_id)
    if user is None:
        raise RuntimeError(f"user {user_id} not found")

    # Seed total once at start. Re-counting every iteration would burn
    # CPU and the headline number jumping around is worse UX than a
    # fixed denominator that done occasionally edges past (skipped
    # readonly photos shrink the real workload after the fact).
    total = _compute_total(db)
    with SessionLocal() as ps:
        jobs_mod.set_progress(ps, job_id, done=0, total=total)
    if total == 0:
        return

    total_trashed = 0
    total_skipped = 0
    iterations = 0
    MAX_ITER = 10000  # same upper bound as the old client loop

    while iterations < MAX_ITER:
        iterations += 1

        # Cancel check between iterations — keeps reaction time under
        # ~1 chunk's worth of work even on a long run.
        with SessionLocal() as cs:
            if jobs_mod.is_cancelled(cs, job_id):
                log.info("dedup_cleanup job %d cancelled at iter %d", job_id, iterations)
                return

        ids, group_count = _next_chunk_ids(db, PAGE_SIZE)
        if not ids:
            return

        iter_trashed = 0
        iter_skipped = 0
        for off in range(0, len(ids), CHUNK):
            slice_ids = ids[off:off + CHUNK]
            result = trash_photos_core(db, slice_ids, user)
            iter_trashed += int(result.get("deleted") or 0)
            iter_skipped += (
                len(result.get("skipped_readonly") or [])
                + len(result.get("failed") or [])
            )
            total_trashed += int(result.get("deleted") or 0)
            total_skipped += (
                len(result.get("skipped_readonly") or [])
                + len(result.get("failed") or [])
            )
            with SessionLocal() as ps:
                jobs_mod.set_progress(ps, job_id, done=total_trashed)

            with SessionLocal() as cs:
                if jobs_mod.is_cancelled(cs, job_id):
                    log.info(
                        "dedup_cleanup job %d cancelled mid-chunk (%d trashed)",
                        job_id, total_trashed,
                    )
                    return

        # Termination guard — if a pass trashed nothing but skipped some
        # rows, the same readonly/failed groups will reappear next loop
        # and we'd spin forever. Same logic the client used.
        if iter_trashed == 0 and iter_skipped > 0:
            raise RuntimeError(
                f"남은 {iter_skipped}장은 read-only / 권한 문제로 "
                f"자동정리 대상이 아닙니다",
            )

    log.warning("dedup_cleanup job %d hit iteration cap %d", job_id, MAX_ITER)

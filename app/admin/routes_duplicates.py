"""Admin endpoints for duplicate-file management.

Photos with the same sha256 but different (root, rel_path) are "duplicates"
in catalog terms — they point at byte-identical files stored in more than
one location. The thumbnail cache already dedupes (paths are keyed on
sha256), so eliminating duplicate rows is a catalog-tidiness gain rather
than disk-reclaim, unless the user also lets the worker actually trash
the underlying file (which it does — moves to data/trash/).
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import String as sa_String
from sqlalchemy import asc, desc, func, literal, select, update
from sqlalchemy.orm import Session

from ..api.deps import get_db
from ..auth import require_admin
from ..models import Job, Photo, Root, User
from ..worker.jobs import enqueue

router = APIRouter(prefix="/admin/duplicates", tags=["admin", "duplicates"])


class DupStats(BaseModel):
    groups: int           # number of sha256s with more than one photo
    duplicate_rows: int   # sum of (count - 1) across groups
    wasted_bytes: int     # sum of (count - 1) * file_size


class DupMember(BaseModel):
    id: int
    root_id: int
    root_label: str
    # Exposed so the admin UI can disable the per-group trash button
    # when every member sits on a readonly root (the bulk-delete would
    # come back as all skipped_readonly anyway).
    root_readonly: bool
    rel_path: str
    filename: str
    taken_at: datetime | None = None
    # Extra context shown in each card so the admin can pick which
    # copy to keep with more than the path to go on (camera body
    # is the biggest tell — the older copy from the camera vs. a
    # shrunk-down social-media re-share).
    mtime: datetime | None = None
    width: int | None = None
    height: int | None = None
    camera_model: str | None = None


class DupGroup(BaseModel):
    sha256: str
    count: int
    file_size: int | None = None
    photos: list[DupMember]


class DupGroupPage(BaseModel):
    total_groups: int
    page: int
    page_size: int
    items: list[DupGroup]


def _dup_subquery():
    """Subquery: one row per sha256 that has 2+ active photos *with a
    usable thumbnail*.

    The thumb_status gate matters: photos whose original file was
    deleted from disk directly (i.e. not through the trash UI) stay
    status='active' in the DB until a full scan reconciles them as
    missing. Those rows have no thumbnail on disk either, so showing
    them in the dup view just produces a wall of 404s in the grid.
    Filtering on ok/partial drops them from the dup count immediately
    — once reconciliation runs they'll flip to 'missing' and the
    status filter will exclude them permanently.
    """
    # `dir_variants` counts distinct (root_id, parent-folder) tuples
    # for a sha. When = 1, every copy lives in the same folder — the
    # highest-cleanup-value case (typically accidental re-imports that
    # left IMG_001.jpg / IMG_001 (1).jpg / IMG_001_copy.jpg side by
    # side). The list endpoint sorts those groups to the top so the
    # admin can knock them out first.
    #
    # Parent path is computed as rel_path with the trailing filename
    # stripped — `LENGTH(rel_path) - LENGTH(filename)` is exactly the
    # parent length (handles "subdir/file" → "subdir/", "file" → "").
    parent = func.substr(
        Photo.rel_path, 1, func.length(Photo.rel_path) - func.length(Photo.filename),
    )
    dir_key = (
        func.cast(Photo.root_id, sa_String) + literal("|") + parent
    )
    return (
        select(
            Photo.sha256.label("sha256"),
            func.count(Photo.id).label("n"),
            func.max(Photo.file_size).label("file_size"),
            func.count(func.distinct(dir_key)).label("dir_variants"),
            # Group's most-recent taken_at, used as the final sort tier
            # (recent shots surfaced first so the admin tackles fresh
            # imports while the context is still in their head).
            func.max(Photo.taken_at).label("max_taken_at"),
        )
        .where(
            Photo.sha256.is_not(None),
            Photo.status == "active",
            Photo.thumb_status.in_(("ok", "partial")),
        )
        .group_by(Photo.sha256)
        .having(func.count(Photo.id) > 1)
    )


@router.get("/stats", response_model=DupStats)
def stats(
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
) -> DupStats:
    subq = _dup_subquery().subquery()
    row = db.execute(
        select(
            func.count(subq.c.sha256),
            func.coalesce(func.sum(subq.c.n - 1), 0),
            func.coalesce(func.sum((subq.c.n - 1) * subq.c.file_size), 0),
        )
    ).one()
    return DupStats(
        groups=int(row[0] or 0),
        duplicate_rows=int(row[1] or 0),
        wasted_bytes=int(row[2] or 0),
    )


@router.get("/groups", response_model=DupGroupPage)
def list_groups(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
) -> DupGroupPage:
    base = _dup_subquery()
    total = db.execute(
        select(func.count()).select_from(base.subquery())
    ).scalar_one()
    if total == 0:
        return DupGroupPage(total_groups=0, page=page, page_size=page_size, items=[])

    # Sort priority (per user spec):
    #   1. same-folder groups first  (dir_variants ASC → 1 first)
    #   2. larger files first        (file_size DESC)
    #   3. most-recent shot first    (max_taken_at DESC NULLS LAST)
    #   4. sha256 for deterministic pagination on full ties.
    from sqlalchemy import column as sa_column
    page_q = (
        base.order_by(
            asc("dir_variants"),
            desc("file_size"),
            sa_column("max_taken_at").desc().nullslast(),
            Photo.sha256,
        )
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    rows = db.execute(page_q).all()
    if not rows:
        return DupGroupPage(total_groups=total, page=page, page_size=page_size, items=[])

    shas = [r[0] for r in rows]
    # Sort so the "first" item per group (the suggested keep) is the
    # OLDEST instance of the file — usually the original, before
    # someone copied it into a second folder. Falls back to mtime
    # then to alphabetical so the order stays deterministic even
    # when taken_at / mtime are missing on every row.
    members_rows = db.execute(
        select(
            Photo.id, Photo.sha256, Photo.root_id, Root.label, Root.readonly,
            Photo.rel_path, Photo.filename, Photo.taken_at, Photo.mtime,
            Photo.width, Photo.height, Photo.camera_model,
        )
        .join(Root, Root.id == Photo.root_id)
        .where(
            Photo.sha256.in_(shas),
            Photo.status == "active",
            Photo.thumb_status.in_(("ok", "partial")),
        )
        .order_by(
            Photo.sha256,
            Photo.taken_at.asc().nullslast(),
            Photo.mtime.asc().nullslast(),
            Root.label,
            Photo.rel_path,
        )
    ).all()
    by_sha: dict[str, list[DupMember]] = {}
    for r in members_rows:
        by_sha.setdefault(r[1], []).append(
            DupMember(
                id=r[0], root_id=r[2], root_label=r[3], root_readonly=bool(r[4]),
                rel_path=r[5], filename=r[6], taken_at=r[7],
                mtime=r[8], width=r[9], height=r[10], camera_model=r[11],
            )
        )

    items = [
        DupGroup(
            sha256=r[0], count=int(r[1]), file_size=r[2],
            photos=by_sha.get(r[0], []),
        )
        for r in rows
    ]
    return DupGroupPage(total_groups=total, page=page, page_size=page_size, items=items)


# ---------- Auto-cleanup job control ----------

class CleanupStart(BaseModel):
    status: str          # "started" | "already_running"
    job_id: int


class CleanupStatus(BaseModel):
    # "idle" when no dedup job has ever run; otherwise the latest job's
    # state. The admin UI shows a progress bar only when status is
    # "queued" or "running".
    status: str
    job_id: int | None = None
    progress_done: int = 0
    progress_total: int = 0
    started_at: datetime | None = None
    finished_at: datetime | None = None
    last_error: str | None = None


def _latest_dedup_job(db: Session) -> Job | None:
    return db.execute(
        select(Job).where(Job.kind == "dedup_cleanup")
        .order_by(Job.id.desc()).limit(1)
    ).scalar_one_or_none()


def _live_dedup_job(db: Session) -> Job | None:
    return db.execute(
        select(Job).where(
            Job.kind == "dedup_cleanup",
            Job.status.in_(("queued", "running")),
        ).order_by(Job.id.desc()).limit(1)
    ).scalar_one_or_none()


@router.post("/auto-cleanup", response_model=CleanupStart)
def start_auto_cleanup(
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
) -> CleanupStart:
    """Enqueue a dedup_cleanup job and return immediately. Refuses if
    one is already queued/running — there's no upside to two workers
    racing on the same dup list."""
    live = _live_dedup_job(db)
    if live is not None:
        return CleanupStart(status="already_running", job_id=live.id)
    job_id = enqueue(db, kind="dedup_cleanup", payload={"user_id": user.id}, priority=3)
    db.commit()
    return CleanupStart(status="started", job_id=job_id)


@router.get("/cleanup-status", response_model=CleanupStatus)
def get_cleanup_status(
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
) -> CleanupStatus:
    job = _latest_dedup_job(db)
    if job is None:
        return CleanupStatus(status="idle")
    return CleanupStatus(
        status=job.status,
        job_id=job.id,
        progress_done=job.progress_done,
        progress_total=job.progress_total,
        started_at=job.started_at,
        finished_at=job.finished_at,
        last_error=job.last_error,
    )


# ---------- Right-edge minimap histogram ----------

class YearBucket(BaseModel):
    # `year=None` means "no taken_at". Stays a separate bucket so the
    # client can label it explicitly rather than dropping into "0".
    year: int | None
    count: int


@router.get("/year-histogram", response_model=list[YearBucket])
def year_histogram(
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
) -> list[YearBucket]:
    """Run-length-encoded year list across the duplicate groups, in the
    same order /groups uses. Each bucket is one stretch of adjacent
    groups sharing a representative year (= group's max taken_at year).
    Same year can appear multiple times because the sort key is mixed
    (dir_variants → file_size → max_taken_at), so the minimap reads as
    "this region of the list is dominated by year X" rather than as a
    clean histogram.
    """
    from sqlalchemy import column as sa_column
    base = _dup_subquery()
    rows = db.execute(
        base.order_by(
            asc("dir_variants"),
            desc("file_size"),
            sa_column("max_taken_at").desc().nullslast(),
            Photo.sha256,
        )
    ).all()
    buckets: list[YearBucket] = []
    sentinel = object()
    cur: object | int | None = sentinel
    cnt = 0
    for r in rows:
        # _dup_subquery selects max_taken_at as the 5th column (after
        # sha256, n, file_size, dir_variants). datetime → year or None.
        mt = r[4]
        y = mt.year if mt is not None else None
        if y == cur:
            cnt += 1
        else:
            if cur is not sentinel:
                buckets.append(YearBucket(year=cur, count=cnt))  # type: ignore[arg-type]
            cur = y
            cnt = 1
    if cur is not sentinel and cnt > 0:
        buckets.append(YearBucket(year=cur, count=cnt))  # type: ignore[arg-type]
    return buckets


@router.post("/auto-cleanup/cancel")
def cancel_auto_cleanup(
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
) -> dict:
    """Mark any live dedup_cleanup job as cancelled. The worker polls
    between chunks and bails on next check — already-trashed photos
    stay trashed."""
    n = db.execute(
        update(Job).where(
            Job.kind == "dedup_cleanup",
            Job.status.in_(("queued", "running")),
        ).values(status="cancelled", finished_at=datetime.utcnow(), claim_token=None)
    ).rowcount
    db.commit()
    return {"cancelled": int(n or 0)}

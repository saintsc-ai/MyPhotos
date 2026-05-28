"""Minimal jobs visibility — see queue depth and recent failures."""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import delete as _delete
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..api.deps import get_db
from ..models import Job

router = APIRouter(prefix="/admin/jobs", tags=["admin", "jobs"])

# Statuses safe to wipe by default. `running` requires opt-in because a
# worker may still be mid-job — re-claiming it would clash on the
# claim_token. `done` is excluded so audit trail is not silently lost.
_PURGEABLE_DEFAULT = {"queued", "failed"}
_PURGEABLE_ALL = {"queued", "failed", "running", "done"}


class JobStats(BaseModel):
    queued: int
    running: int
    failed: int
    done: int


class JobOut(BaseModel):
    id: int
    kind: str
    status: str
    attempts: int
    last_error: str | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None

    class Config:
        from_attributes = True


@router.get("/stats", response_model=JobStats)
def stats(db: Session = Depends(get_db)) -> JobStats:
    rows = dict(
        db.execute(select(Job.status, func.count(Job.id)).group_by(Job.status)).all()
    )
    return JobStats(
        queued=rows.get("queued", 0),
        running=rows.get("running", 0),
        failed=rows.get("failed", 0),
        done=rows.get("done", 0),
    )


@router.get("/recent", response_model=list[JobOut])
def recent(
    status_filter: str | None = None,
    limit: int = 50,
    db: Session = Depends(get_db),
) -> list[JobOut]:
    q = select(Job).order_by(Job.id.desc()).limit(min(limit, 500))
    if status_filter:
        q = q.where(Job.status == status_filter)
    return [JobOut.model_validate(r) for r in db.execute(q).scalars().all()]


class PhotoIndexStats(BaseModel):
    """Indexing progress per stage. All counts scope to active photos only."""

    total_active: int
    exif_pending: int
    exif_ok: int
    exif_partial: int
    exif_failed: int
    exif_skipped: int
    thumb_pending: int
    thumb_ok: int
    thumb_failed: int
    thumb_skipped: int


@router.get("/photo-stats", response_model=PhotoIndexStats)
def photo_stats(db: Session = Depends(get_db)) -> PhotoIndexStats:
    """Per-stage counts for the indexing dashboard.

    Mirrors the EXIF/thumb status fields on Photo so the admin UI can
    show a progress bar (`ok / total`) and how many retries are pending.
    """
    from ..models import Photo

    exif = dict(
        db.execute(
            select(Photo.exif_status, func.count(Photo.id))
            .where(Photo.status == "active")
            .group_by(Photo.exif_status)
        ).all()
    )
    thumb = dict(
        db.execute(
            select(Photo.thumb_status, func.count(Photo.id))
            .where(Photo.status == "active")
            .group_by(Photo.thumb_status)
        ).all()
    )
    total = sum(exif.values())
    return PhotoIndexStats(
        total_active=total,
        exif_pending=exif.get("pending", 0),
        exif_ok=exif.get("ok", 0),
        exif_partial=exif.get("partial", 0),
        exif_failed=exif.get("failed", 0),
        exif_skipped=exif.get("skipped", 0),
        thumb_pending=thumb.get("pending", 0),
        thumb_ok=thumb.get("ok", 0),
        thumb_failed=thumb.get("failed", 0),
        thumb_skipped=thumb.get("skipped", 0),
    )


class RetryRequest(BaseModel):
    # Which stage(s) to retry. 'exif' or 'thumb' (or both).
    stages: list[str] = ["exif", "thumb"]
    # Photo filter — re-enqueue every photo whose status is in the list.
    # e.g. ['failed', 'partial'] to retry both failure modes at once.
    exif_status: list[str] = []
    thumb_status: list[str] = []
    root_id: int | None = None
    limit: int = 1000


class RetryResponse(BaseModel):
    matched: int
    enqueued: int


class PurgeRequest(BaseModel):
    """Body for /api/admin/jobs/purge.

    `statuses` defaults to {queued, failed}. To also wipe `running` jobs
    (e.g. when a stuck job is blocking the queue after a misconfigured
    earlier run), set `include_running=True`. `kind` further narrows the
    delete to a single job kind (e.g. 'discover_root', 'index_file',
    'classify_ml').
    """

    statuses: list[str] | None = None
    include_running: bool = False
    kind: str | None = None


class PurgeResponse(BaseModel):
    deleted: int
    statuses: list[str]
    kind: str | None


@router.post("/purge", response_model=PurgeResponse)
def purge_jobs(body: PurgeRequest | None = None, db: Session = Depends(get_db)) -> PurgeResponse:
    """Delete jobs matching the given statuses (default queued+failed).

    Use case: an earlier scan was launched with a wrong root path or
    permissions, the original jobs failed but new attempts can't start
    because the queue is still full of dead entries. Purge clears them
    so a fresh scan can run.
    """
    req = body or PurgeRequest()
    statuses = set(req.statuses or _PURGEABLE_DEFAULT)
    if req.include_running:
        statuses.add("running")
    unknown = statuses - _PURGEABLE_ALL
    if unknown:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"알 수 없는 상태: {sorted(unknown)} (허용: {sorted(_PURGEABLE_ALL)})",
        )
    if not statuses:
        return PurgeResponse(deleted=0, statuses=[], kind=req.kind)

    stmt = _delete(Job).where(Job.status.in_(statuses))
    if req.kind:
        stmt = stmt.where(Job.kind == req.kind)
    result = db.execute(stmt)
    db.commit()
    return PurgeResponse(
        deleted=int(result.rowcount or 0),
        statuses=sorted(statuses),
        kind=req.kind,
    )


class PairCompanionsResponse(BaseModel):
    scanned: int
    paired: int


@router.post("/pair-companions", response_model=PairCompanionsResponse)
def pair_companions(db: Session = Depends(get_db)) -> PairCompanionsResponse:
    """One-shot backfill: walk every still-unpaired photo and try to link
    HEIC↔MOV (or other image↔video) pairs that share root + parent dir +
    filename stem and were created within ~24 h of each other.

    Idempotent. Safe to re-run after a re-scan adds more files.
    """
    from ..models import Photo
    from ..scanner.discover import _LIVE_STILL_EXTS, _LIVE_VIDEO_EXTS

    def _live_eligible(p) -> bool:
        ext = (p.ext or "").lower().lstrip(".")
        if p.media_kind == "image":
            return ext in _LIVE_STILL_EXTS
        return ext in _LIVE_VIDEO_EXTS

    def _live_pair(a, b) -> bool:
        # One must be image-kind with a still ext, the other video-
        # kind with the video ext. Order-agnostic.
        if a.media_kind == "image" and b.media_kind == "video":
            still, vid = a, b
        elif a.media_kind == "video" and b.media_kind == "image":
            still, vid = b, a
        else:
            return False
        still_ext = (still.ext or "").lower().lstrip(".")
        vid_ext = (vid.ext or "").lower().lstrip(".")
        return still_ext in _LIVE_STILL_EXTS and vid_ext in _LIVE_VIDEO_EXTS

    scanned = 0
    paired = 0
    rows = db.execute(
        select(Photo).where(Photo.companion_id.is_(None))
    ).scalars().all()
    # In-memory index keyed by (root_id, parent_dir, stem) so we don't
    # query the DB once per photo (would be ~10⁵ selects on a real
    # library). Each entry maps the key to the first photo seen there.
    index: dict[tuple[int, str, str], Photo] = {}
    for p in rows:
        scanned += 1
        name = p.filename or ""
        if "." not in name:
            continue
        stem = name.rsplit(".", 1)[0]
        if not stem:
            continue
        # Cheap ext gate before bothering the index — only iPhone Live
        # Photo extensions ever pair.
        if not _live_eligible(p):
            continue
        parent = p.rel_path.rsplit("/", 1)[0] if "/" in p.rel_path else ""
        key = (p.root_id, parent, stem)
        partner = index.get(key)
        if partner is None:
            index[key] = p
            continue
        # Two photos with the same stem in the same dir — pair only if
        # they form a valid Live Photo (HEIC|JPG ↔ MOV) and were
        # written within ~24h of each other.
        if not _live_pair(partner, p):
            index[key] = p
            continue
        if partner.mtime and p.mtime:
            if abs((partner.mtime - p.mtime).total_seconds()) > 86400:
                index[key] = p
                continue
        p.companion_id = partner.id
        partner.companion_id = p.id
        paired += 1
        # Now that both are paired, evict from the index so a fourth
        # file at the same key doesn't accidentally re-pair.
        index.pop(key, None)
    db.commit()
    return PairCompanionsResponse(scanned=scanned, paired=paired)


class FailedPhotoOut(BaseModel):
    id: int
    root_label: str
    rel_path: str
    filename: str
    media_kind: str
    exif_status: str
    thumb_status: str
    classify_status: str
    exif_error: str | None = None
    thumb_error: str | None = None
    error: str | None = None      # whichever stage's error fits the filter


class FailedPhotosPage(BaseModel):
    total: int
    page: int
    page_size: int
    items: list[FailedPhotoOut]


@router.get("/failed-photos", response_model=FailedPhotosPage)
def failed_photos(
    stage: str = "exif",
    page: int = 1,
    page_size: int = 100,
    db: Session = Depends(get_db),
) -> FailedPhotosPage:
    """List photos whose pipeline stage is `failed`, with the worker's
    error message. Admin uses this to spot junk files (wrong format,
    permission issues, corrupt headers) and bulk-delete them.

    stage must be one of `exif`, `thumb`, `classify`.
    """
    from ..models import Photo, Root

    page = max(1, page)
    page_size = max(1, min(page_size, 500))
    stage = stage.lower()
    if stage not in ("exif", "thumb", "classify"):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "stage는 exif / thumb / classify 중 하나여야 합니다",
        )
    status_col = {
        "exif": Photo.exif_status,
        "thumb": Photo.thumb_status,
        "classify": Photo.classify_status,
    }[stage]

    base = (
        select(Photo, Root.label)
        .join(Root, Root.id == Photo.root_id)
        .where(Photo.status == "active", status_col == "failed")
    )
    total = db.execute(
        select(func.count()).select_from(base.subquery())
    ).scalar_one()
    rows = db.execute(
        base.order_by(Photo.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    ).all()
    items: list[FailedPhotoOut] = []
    for p, root_label in rows:
        # classify_status has no dedicated error column on Photo; reuse
        # exif_error / thumb_error for those stages and fall back to None.
        primary_err = (
            p.exif_error if stage == "exif"
            else p.thumb_error if stage == "thumb"
            else None
        )
        items.append(FailedPhotoOut(
            id=p.id,
            root_label=root_label,
            rel_path=p.rel_path,
            filename=p.filename,
            media_kind=p.media_kind,
            exif_status=p.exif_status,
            thumb_status=p.thumb_status,
            classify_status=p.classify_status,
            exif_error=p.exif_error,
            thumb_error=p.thumb_error,
            error=primary_err,
        ))
    return FailedPhotosPage(
        total=int(total or 0), page=page, page_size=page_size, items=items,
    )


@router.post("/retry-photos", response_model=RetryResponse)
def retry_photos(body: RetryRequest, db: Session = Depends(get_db)) -> RetryResponse:
    """Reset selected photos' stage status to 'pending' and enqueue
    index_file jobs for them. Useful after installing exiftool/ffmpeg/
    pillow-heif to recover previously failed extractions.
    """
    from sqlalchemy import update

    from ..models import Photo
    from ..worker.jobs import enqueue_many

    q = select(Photo.id)
    if body.root_id is not None:
        q = q.where(Photo.root_id == body.root_id)
    if body.exif_status:
        q = q.where(Photo.exif_status.in_(body.exif_status))
    if body.thumb_status:
        q = q.where(Photo.thumb_status.in_(body.thumb_status))
    q = q.limit(min(body.limit, 100_000))

    photo_ids = [r[0] for r in db.execute(q).all()]
    if not photo_ids:
        return RetryResponse(matched=0, enqueued=0)

    reset_values: dict = {}
    if "exif" in body.stages:
        reset_values["exif_status"] = "pending"
        reset_values["exif_error"] = None
    if "thumb" in body.stages:
        reset_values["thumb_status"] = "pending"
        reset_values["thumb_error"] = None

    # Chunk the work so the SQLite writer lock is released between batches —
    # the indexing/ML workers are continuously writing, and a single 1000-row
    # UPDATE + 1000-row INSERT in one transaction can starve them out and
    # itself hit `database is locked` after the busy_timeout.
    CHUNK = 200
    enqueued = 0
    for i in range(0, len(photo_ids), CHUNK):
        chunk = photo_ids[i:i + CHUNK]
        if reset_values:
            db.execute(update(Photo).where(Photo.id.in_(chunk)).values(**reset_values))
        enqueued += enqueue_many(
            db,
            kind="index_file",
            payloads=[{"photo_id": pid} for pid in chunk],
            priority=5,
        )
        db.commit()
    return RetryResponse(matched=len(photo_ids), enqueued=enqueued)

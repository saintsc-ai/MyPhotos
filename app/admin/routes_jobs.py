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


class PhotoKindCount(BaseModel):
    kind: str  # "image" | "video"
    count: int


class PhotoRootCount(BaseModel):
    root_id: int
    label: str
    count: int


class PhotoDistribution(BaseModel):
    """Active-photo counts split two ways for dashboard donuts."""

    by_kind: list[PhotoKindCount]
    by_root: list[PhotoRootCount]


@router.get("/photo-distribution", response_model=PhotoDistribution)
def photo_distribution(db: Session = Depends(get_db)) -> PhotoDistribution:
    """How active photos break down by media kind and by root.

    Two cheap GROUP BY queries — cheaper than the per-stage stats above
    since neither key needs status filtering. Drives the media-kind and
    per-root donuts on the indexing tab.
    """
    from ..models import Photo, Root

    by_kind_rows = db.execute(
        select(Photo.media_kind, func.count(Photo.id))
        .where(Photo.status == "active")
        .group_by(Photo.media_kind)
        .order_by(func.count(Photo.id).desc())
    ).all()
    by_root_rows = db.execute(
        select(Root.id, Root.label, func.count(Photo.id))
        .join(Photo, Photo.root_id == Root.id)
        .where(Photo.status == "active")
        .group_by(Root.id, Root.label)
        .order_by(func.count(Photo.id).desc())
    ).all()
    return PhotoDistribution(
        by_kind=[PhotoKindCount(kind=k or "unknown", count=c) for k, c in by_kind_rows],
        by_root=[
            PhotoRootCount(root_id=rid, label=label or f"root#{rid}", count=c)
            for rid, label, c in by_root_rows
        ],
    )


class RetryRequest(BaseModel):
    # Which stage(s) to retry. 'exif' or 'thumb' (or both).
    stages: list[str] = ["exif", "thumb"]
    # Photo filter — re-enqueue every photo whose status matches.
    exif_status: str | None = None  # e.g. 'failed', 'partial'
    thumb_status: str | None = None
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


@router.post("/retry-photos", response_model=RetryResponse)
def retry_photos(body: RetryRequest, db: Session = Depends(get_db)) -> RetryResponse:
    """Reset selected photos' stage status to 'pending' and enqueue
    index_file jobs for them. Useful after installing exiftool/ffmpeg/
    pillow-heif to recover previously failed extractions.
    """
    from sqlalchemy import update

    from ..models import Photo
    from ..worker.jobs import enqueue

    q = select(Photo.id)
    if body.root_id is not None:
        q = q.where(Photo.root_id == body.root_id)
    if body.exif_status is not None:
        q = q.where(Photo.exif_status == body.exif_status)
    if body.thumb_status is not None:
        q = q.where(Photo.thumb_status == body.thumb_status)
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
    if reset_values:
        db.execute(update(Photo).where(Photo.id.in_(photo_ids)).values(**reset_values))

    enqueued = 0
    for pid in photo_ids:
        enqueue(db, kind="index_file", payload={"photo_id": pid}, priority=5)
        enqueued += 1
    db.commit()
    return RetryResponse(matched=len(photo_ids), enqueued=enqueued)

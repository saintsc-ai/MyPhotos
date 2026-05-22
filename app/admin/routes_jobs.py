"""Minimal jobs visibility — see queue depth and recent failures."""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..api.deps import get_db
from ..models import Job

router = APIRouter(prefix="/admin/jobs", tags=["admin", "jobs"])


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
    # Photo filter — re-enqueue every photo whose status matches.
    exif_status: str | None = None  # e.g. 'failed', 'partial'
    thumb_status: str | None = None
    root_id: int | None = None
    limit: int = 1000


class RetryResponse(BaseModel):
    matched: int
    enqueued: int


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

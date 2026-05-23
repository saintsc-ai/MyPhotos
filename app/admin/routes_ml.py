"""Admin endpoints for the ML pipeline.

GET  /api/admin/ml/stats       — classify_status breakdown (pending/ok/failed)
POST /api/admin/ml/enqueue     — bulk-enqueue classify_objects jobs for
                                  photos that haven't been classified yet
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..api.deps import get_db
from ..models import Photo, PhotoTag, Tag
from ..worker.jobs import enqueue

router = APIRouter(prefix="/admin/ml", tags=["admin", "ml"])


class MLStats(BaseModel):
    classify_pending: int
    classify_ok: int
    classify_failed: int
    classify_skipped: int
    auto_tag_count: int   # photos that ended up with at least one auto-* tag


@router.get("/stats", response_model=MLStats)
def ml_stats(db: Session = Depends(get_db)) -> MLStats:
    counts = dict(
        db.execute(
            select(Photo.classify_status, func.count(Photo.id))
            .where(Photo.status == "active")
            .group_by(Photo.classify_status)
        ).all()
    )
    # How many photos actually got auto tags attached. A photo can be
    # classify_status=ok but have zero auto-* tags (e.g. blurred photo
    # with no detections above threshold).
    auto_tagged = db.execute(
        select(func.count(func.distinct(PhotoTag.photo_id)))
        .join(Tag, Tag.id == PhotoTag.tag_id)
        .where(Tag.source.like("auto-%"))
    ).scalar_one() or 0
    return MLStats(
        classify_pending=counts.get("pending", 0),
        classify_ok=counts.get("ok", 0),
        classify_failed=counts.get("failed", 0),
        classify_skipped=counts.get("skipped", 0),
        auto_tag_count=int(auto_tagged),
    )


class EnqueueRequest(BaseModel):
    # If True, also re-enqueue photos that already have classify_status='ok'.
    # Useful after swapping models.
    force_reclassify: bool = False
    # Only consider photos whose thumb is ready (so YOLO has something to read).
    only_with_thumbs: bool = True
    limit: int = 100_000


class EnqueueResult(BaseModel):
    matched: int
    enqueued: int


@router.post("/enqueue", response_model=EnqueueResult)
def enqueue_classify(
    body: EnqueueRequest,
    db: Session = Depends(get_db),
) -> EnqueueResult:
    q = select(Photo.id).where(Photo.status == "active")
    if not body.force_reclassify:
        q = q.where(Photo.classify_status != "ok")
    if body.only_with_thumbs:
        q = q.where(Photo.thumb_status.in_(("ok", "partial")))
    q = q.limit(min(body.limit, 1_000_000))

    photo_ids = [r[0] for r in db.execute(q).all()]
    if not photo_ids:
        return EnqueueResult(matched=0, enqueued=0)

    # Mark as pending so retried 'failed' photos get a fresh attempt and
    # the stats reflect what's in flight.
    for pid in photo_ids:
        # claim already increments attempts; resetting classify_status is
        # enough to ride the same flow.
        db.execute(
            Photo.__table__.update()
            .where(Photo.id == pid)
            .values(classify_status="pending")
        )
        enqueue(db, kind="classify_objects", payload={"photo_id": pid}, priority=3)
    db.commit()
    return EnqueueResult(matched=len(photo_ids), enqueued=len(photo_ids))

"""Admin endpoints for the ML pipeline.

GET  /api/admin/ml/stats         — classify_status + auto-tag counts
POST /api/admin/ml/enqueue       — bulk-enqueue classify jobs for any
                                    requested subset of {objects, embedding, faces}
GET  /api/admin/ml/clusters      — list face clusters (named + unnamed)
PATCH /api/admin/ml/clusters/{id} — rename or merge a cluster
DELETE /api/admin/ml/clusters/{id} — drop a cluster (faces become unassigned)
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import and_, func, or_, select, true, update
from sqlalchemy.orm import Session

from ..api.deps import get_db
from ..models import FaceCluster, Job, Photo, PhotoFace
from ..worker.jobs import enqueue, enqueue_unique_for_photo
from ..api.deps import get_db

router = APIRouter(prefix="/admin/ml", tags=["admin", "ml"])

log = logging.getLogger(__name__)


# --- stats ----------------------------------------------------------------


class MLStats(BaseModel):
    classify_pending: int
    classify_ok: int
    classify_failed: int
    classify_skipped: int
    auto_tag_count: int
    clip_embedded: int          # photos with stored CLIP embedding
    faces_detected: int         # total face rows
    face_cluster_total: int
    face_cluster_named: int


@router.get("/stats", response_model=MLStats)
def ml_stats(db: Session = Depends(get_db)) -> MLStats:
    counts = dict(
        db.execute(
            select(Photo.classify_status, func.count(Photo.id))
            .where(Photo.status == "active")
            .group_by(Photo.classify_status)
        ).all()
    )
    # After the photo_auto_tags split, ML labels live in their own
    # table — count distinct photos that have at least one auto label.
    from ..models import PhotoAutoTag
    auto_tagged = db.execute(
        select(func.count(func.distinct(PhotoAutoTag.photo_id)))
    ).scalar_one() or 0

    try:
        clip_embedded = db.execute(
            select(func.count()).select_from(
                # one-row probe rather than importing PhotoEmbedding here
                func.coalesce.__class__   # sentinel; will use SQL count below
            )
        )
    except Exception:
        clip_embedded = 0
    # cleaner — separate query
    from ..models import PhotoEmbedding
    try:
        clip_embedded = db.execute(
            select(func.count(PhotoEmbedding.photo_id))
        ).scalar_one() or 0
    except Exception:
        clip_embedded = 0
    try:
        faces_detected = db.execute(
            select(func.count(PhotoFace.id))
        ).scalar_one() or 0
        face_cluster_total = db.execute(
            select(func.count(FaceCluster.id))
        ).scalar_one() or 0
        face_cluster_named = db.execute(
            select(func.count(FaceCluster.id)).where(FaceCluster.label.is_not(None))
        ).scalar_one() or 0
    except Exception:
        faces_detected = face_cluster_total = face_cluster_named = 0

    return MLStats(
        classify_pending=counts.get("pending", 0),
        classify_ok=counts.get("ok", 0),
        classify_failed=counts.get("failed", 0),
        classify_skipped=counts.get("skipped", 0),
        auto_tag_count=int(auto_tagged),
        clip_embedded=int(clip_embedded),
        faces_detected=int(faces_detected),
        face_cluster_total=int(face_cluster_total),
        face_cluster_named=int(face_cluster_named),
    )


# --- enqueue --------------------------------------------------------------


# Each requested stage maps to its own Photo status column.
_STAGE_TO_COL = {
    "objects": "objects_status",
    "embedding": "clip_status",
    "faces": "faces_status",
    "ocr": "ocr_status",
}
_DONE_CLASSIFY = ("ok", "skipped")
_DONE_OCR = ("ok", "empty", "skipped")


class EnqueueRequest(BaseModel):
    # Subset of {'objects', 'embedding', 'faces', 'ocr'}. Default = the
    # three classify stages (OCR is opt-in — heavier, search-only).
    stages: list[str] = ["objects", "embedding", "faces"]
    force_reclassify: bool = False    # re-enqueue even photos already 'ok'
    only_with_thumbs: bool = True
    limit: int = 200_000


class EnqueueResult(BaseModel):
    matched: int
    enqueued: int
    by_stage: dict[str, int]


@router.post("/enqueue", response_model=EnqueueResult)
def enqueue_classify(
    body: EnqueueRequest,
    db: Session = Depends(get_db),
) -> EnqueueResult:
    stages = [s for s in body.stages if s in _STAGE_TO_COL]
    if not stages:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "stages must include at least one of objects/embedding/faces/ocr",
        )
    cols = [_STAGE_TO_COL[s] for s in stages]
    lim = min(body.limit, 1_000_000)

    # Image = key: toggle ONLY the requested stage column(s) to 'pending' and
    # enqueue ONE classify_ml per photo. The worker runs just the pending
    # stages. A photo matches if ANY requested stage still needs work.
    def _need_cond(col: str):
        attr = getattr(Photo, col)
        if col == "ocr_status":      # nullable, images only, 'empty' counts as done
            base = Photo.media_kind == "image"
            if body.force_reclassify:
                return base
            return and_(base, or_(attr.is_(None), attr.notin_(_DONE_OCR)))
        if body.force_reclassify:
            return true()
        return attr.notin_(_DONE_CLASSIFY)

    q = select(
        Photo.id, Photo.media_kind, Photo.objects_status, Photo.clip_status,
        Photo.faces_status, Photo.ocr_status,
    ).where(Photo.status == "active", or_(*[_need_cond(c) for c in cols]))
    if body.only_with_thumbs:
        q = q.where(Photo.thumb_status.in_(("ok", "partial")))

    enqueued = 0
    for pid, mkind, o_s, c_s, f_s, ocr_s in db.execute(q.limit(lim)).all():
        cur = {"objects_status": o_s, "clip_status": c_s,
               "faces_status": f_s, "ocr_status": ocr_s}
        vals: dict[str, str] = {}
        for col in cols:
            v = cur[col]
            if col == "ocr_status":
                if mkind != "image":
                    continue
                if body.force_reclassify or v not in _DONE_OCR:
                    vals[col] = "pending"
            elif body.force_reclassify or v not in _DONE_CLASSIFY:
                vals[col] = "pending"
        if not vals:
            continue
        # Reset the classify_status roll-up when any classify stage is requeued.
        if any(c in ("objects_status", "clip_status", "faces_status") for c in vals):
            vals["classify_status"] = "pending"
        db.execute(update(Photo).where(Photo.id == pid).values(**vals))
        # enqueue_unique_for_photo: coalesce with any in-flight classify_ml
        # for this photo so a double-click on "분류 시작" doesn't inflate
        # the queue. The worker re-reads photo.*_status when it picks the
        # job up, so newly-toggled stages land on the existing job
        # automatically.
        enqueue_unique_for_photo(db, kind="classify_ml", photo_id=pid, priority=3)
        enqueued += 1

    db.commit()
    return EnqueueResult(
        matched=enqueued, enqueued=enqueued, by_stage={"classify_ml": enqueued},
    )


# --- face clusters --------------------------------------------------------


class ReclusterRequest(BaseModel):
    # Optional overrides — defaults live in worker_ml/faces.py.
    join_threshold: float | None = None
    merge_threshold: float | None = None
    min_face_frac: float | None = None


@router.post("/recluster")
def recluster_faces(body: ReclusterRequest, db: Session = Depends(get_db)) -> dict:
    """Enqueue a full face-cluster rebuild (offline re-clustering over all
    stored embeddings). Returns immediately; the ML worker does the work."""
    active = db.execute(
        select(Job.id).where(
            Job.kind == "recluster_faces",
            Job.status.in_(("queued", "running")),
        ).limit(1)
    ).first()
    if active is not None:
        return {"job_id": active[0], "already_running": True}
    payload = {k: v for k, v in body.model_dump().items() if v is not None}
    job_id = enqueue(db, kind="recluster_faces", payload=payload, priority=4)
    db.commit()
    return {"job_id": job_id, "already_running": False}


class ClusterOut(BaseModel):
    id: int
    label: str | None
    face_count: int


@router.get("/clusters", response_model=list[ClusterOut])
def list_clusters(
    only_named: bool = False,
    min_count: int = 2,
    db: Session = Depends(get_db),
) -> list[ClusterOut]:
    """Face clusters. Defaults: hide singleton clusters (likely noise) and
    show both named + unnamed. Named ones sorted to the top."""
    q = select(FaceCluster).where(FaceCluster.face_count >= min_count)
    if only_named:
        q = q.where(FaceCluster.label.is_not(None))
    q = q.order_by(
        FaceCluster.label.is_(None),   # NULL last (false sorts before true)
        FaceCluster.face_count.desc(),
    )
    rows = db.execute(q).scalars().all()
    return [ClusterOut(id=r.id, label=r.label, face_count=r.face_count) for r in rows]


class ClusterPatchIn(BaseModel):
    label: str | None = None
    merge_into: int | None = None    # optional: move all faces to another cluster id


@router.patch("/clusters/{cluster_id}", response_model=ClusterOut)
def patch_cluster(
    cluster_id: int,
    body: ClusterPatchIn,
    db: Session = Depends(get_db),
) -> ClusterOut:
    c = db.get(FaceCluster, cluster_id)
    if c is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND)

    if body.merge_into is not None and body.merge_into != cluster_id:
        target = db.get(FaceCluster, body.merge_into)
        if target is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "merge target not found")
        # Move face rows.
        moved = db.execute(
            update(PhotoFace)
            .where(PhotoFace.cluster_id == cluster_id)
            .values(cluster_id=target.id)
        ).rowcount or 0
        target.face_count = (target.face_count or 0) + moved
        c.face_count = 0
        # Don't auto-delete the source cluster — admin can DELETE it separately.

    if body.label is not None:
        label = body.label.strip() or None
        if label:
            # Auto-merge: if another cluster already carries this label, treat
            # the rename as "this is the same person as that one" and move
            # all faces over. Returns the surviving (absorbing) cluster so
            # the UI knows which row to highlight after refresh.
            existing = db.execute(
                select(FaceCluster).where(
                    FaceCluster.label == label,
                    FaceCluster.id != cluster_id,
                )
            ).scalar_one_or_none()
            if existing is not None:
                moved = db.execute(
                    update(PhotoFace)
                    .where(PhotoFace.cluster_id == cluster_id)
                    .values(cluster_id=existing.id)
                ).rowcount or 0
                existing.face_count = (existing.face_count or 0) + moved
                c.face_count = 0
                db.commit()
                return ClusterOut(
                    id=existing.id,
                    label=existing.label,
                    face_count=existing.face_count,
                )
        c.label = label

    db.commit()
    return ClusterOut(id=c.id, label=c.label, face_count=c.face_count)


@router.delete("/clusters/{cluster_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_cluster(cluster_id: int, db: Session = Depends(get_db)) -> None:
    c = db.get(FaceCluster, cluster_id)
    if c is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    # ON DELETE SET NULL on photo_faces.cluster_id, so face rows survive.
    db.delete(c)
    db.commit()

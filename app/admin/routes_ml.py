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
from sqlalchemy import func, or_, select, update
from sqlalchemy.orm import Session

from ..api.deps import get_db
from ..models import FaceCluster, Job, Photo, PhotoFace
from ..worker.jobs import enqueue
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


_STAGE_TO_KIND = {
    "objects": "classify_objects",
    "embedding": "classify_embedding",
    "faces": "detect_faces",
}


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
    valid = set(_STAGE_TO_KIND) | {"ocr"}
    stages = [s for s in body.stages if s in valid]
    if not stages:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "stages must include at least one of objects/embedding/faces/ocr",
        )

    classify_stages = [s for s in stages if s in _STAGE_TO_KIND]
    do_ocr = "ocr" in stages
    lim = min(body.limit, 1_000_000)
    by_stage: dict[str, int] = {s: 0 for s in stages}
    touched: set[int] = set()

    # Classify stages (objects/embedding/faces) share classify_status.
    if classify_stages:
        q = select(Photo.id).where(Photo.status == "active")
        if not body.force_reclassify:
            q = q.where(Photo.classify_status != "ok")
        if body.only_with_thumbs:
            q = q.where(Photo.thumb_status.in_(("ok", "partial")))
        for pid in (r[0] for r in db.execute(q.limit(lim)).all()):
            db.execute(
                update(Photo).where(Photo.id == pid).values(classify_status="pending")
            )
            for s in classify_stages:
                enqueue(db, kind=_STAGE_TO_KIND[s], payload={"photo_id": pid}, priority=3)
                by_stage[s] += 1
            touched.add(pid)

    # OCR is a separate axis (ocr_status, images only).
    if do_ocr:
        qo = select(Photo.id).where(
            Photo.status == "active", Photo.media_kind == "image"
        )
        if not body.force_reclassify:
            qo = qo.where(or_(Photo.ocr_status.is_(None), Photo.ocr_status != "ok"))
        if body.only_with_thumbs:
            qo = qo.where(Photo.thumb_status.in_(("ok", "partial")))
        for pid in (r[0] for r in db.execute(qo.limit(lim)).all()):
            db.execute(
                update(Photo).where(Photo.id == pid).values(ocr_status="pending")
            )
            enqueue(db, kind="ocr_text", payload={"photo_id": pid}, priority=3)
            by_stage["ocr"] += 1
            touched.add(pid)

    db.commit()
    return EnqueueResult(
        matched=len(touched),
        enqueued=sum(by_stage.values()),
        by_stage=by_stage,
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

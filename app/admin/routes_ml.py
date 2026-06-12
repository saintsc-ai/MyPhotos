"""Admin endpoints for the ML pipeline.

GET  /api/admin/ml/stats         — classify_status + auto-tag counts
POST /api/admin/ml/enqueue       — bulk-enqueue classify jobs for any
                                    requested subset of {objects, embedding, faces}
GET  /api/admin/ml/clusters      — list face clusters (named + unnamed)
PATCH /api/admin/ml/clusters/{id} — rename or merge a cluster
DELETE /api/admin/ml/clusters/{id} — drop a cluster (faces become unassigned)
"""

from __future__ import annotations

import json
import logging

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import and_, func, or_, select, true, update
from sqlalchemy.orm import Session

from ..api.deps import get_db
from ..models import FaceCluster, Job, Photo, PhotoFace
from ..worker.jobs import enqueue, enqueue_unique_for_photo, recency_priority_boost
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

    # Pull mtime too so we can apply a recency boost per photo. Order
    # DESC so within the same recency tier the newest get the lowest
    # job ids — claim_one's tie-break is `id ASC`, so newer wins.
    q = select(
        Photo.id, Photo.media_kind, Photo.objects_status, Photo.clip_status,
        Photo.faces_status, Photo.ocr_status, Photo.mtime,
    ).where(Photo.status == "active", or_(*[_need_cond(c) for c in cols]))
    if body.only_with_thumbs:
        q = q.where(Photo.thumb_status.in_(("ok", "partial")))
    q = q.order_by(Photo.mtime.desc().nullslast())

    enqueued = 0
    for pid, mkind, o_s, c_s, f_s, ocr_s, mtime in db.execute(q.limit(lim)).all():
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
        # automatically. Recency boost on top of the base 3 — recently-
        # added photos are likely the user's actual target.
        _prio = 3 + recency_priority_boost(mtime)
        enqueue_unique_for_photo(db, kind="classify_ml", photo_id=pid, priority=_prio)
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


# Cosine similarity above this → auto-suggest the matched cluster. SFace
# embeddings (L2-normalised) typically cluster a single person around
# 0.6–0.9; cross-person noise sits at 0.2–0.4. 0.5 splits the two
# distributions safely — above it we're confident enough to offer the
# match as a suggestion (user still has to accept).
_FACE_MATCH_SIM_THRESHOLD = 0.5


class AddFaceIn(BaseModel):
    photo_id: int
    # Normalized [0..1] bbox: [x, y, w, h] in image coords.
    bbox: list[float]
    # Optional explicit choice — bypasses auto-match when provided.
    cluster_id: int | None = None
    # Optional explicit name — find-or-create a cluster with this label.
    # Takes precedence only when cluster_id isn't given.
    label: str | None = None


class AddFaceOut(BaseModel):
    face_id: int
    cluster_id: int
    cluster_label: str | None
    # True when the cluster was picked by embedding similarity (vs the
    # caller's explicit cluster_id / label). The frontend shows this as
    # "이 사람으로 보입니다: <name>" — user can accept or rename to start
    # a new cluster.
    suggested: bool
    # Cosine similarity to the suggested cluster's centroid (None when
    # the caller specified cluster_id / label explicitly).
    suggested_similarity: float | None


@router.post("/faces", response_model=AddFaceOut)
def add_face(body: AddFaceIn, db: Session = Depends(get_db)) -> AddFaceOut:
    """User-drawn face: crop the bbox, embed with SFace, suggest a
    cluster, and insert one PhotoFace row.

    Flow:
      1. Validate bbox + load original image.
      2. Crop to bbox (denormalised), resize to 112×112 BGR for SFace,
         get an L2-normalised 128-d embedding.
      3. Pick the target cluster:
            cluster_id given       → use it
            label given            → find-or-create by label
            otherwise              → compute cosine sim against every
                                     existing cluster's mean embedding,
                                     attach to the best match if it
                                     beats _FACE_MATCH_SIM_THRESHOLD,
                                     else create a fresh unnamed cluster
      4. INSERT PhotoFace + bump cluster.face_count.

    Admin-only (matches the rest of /admin/ml — a manual face add
    affects the cluster everyone sees).
    """
    import numpy as np

    from ..worker_ml import faces as faces_mod
    from ..worker_ml.jobs import _photo_thumb_path

    if len(body.bbox) != 4:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "bbox must be 4 floats")
    x, y, w, h = (float(v) for v in body.bbox)
    if not (0.0 <= x < 1.0 and 0.0 <= y < 1.0 and 0.0 < w <= 1.0 and 0.0 < h <= 1.0):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "bbox out of [0,1] range")
    if w * h < 0.0005:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "박스가 너무 작습니다 (이미지의 0.05% 미만)",
        )

    p = db.get(Photo, body.photo_id)
    if p is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "photo not found")
    if p.media_kind != "image":
        raise HTTPException(
            status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            "동영상엔 얼굴을 추가할 수 없습니다",
        )
    # Use the THUMBNAIL, not the original. Three reasons:
    #   1. RAW formats (PEF, NEF, ARW, CR2, ORF, ...) and HEIC don't open
    #      with PIL.Image.open() — the original add_face implementation
    #      tried, and choked with "cannot identify image file" the moment
    #      a Pentax PEF was selected. Thumbnails are baked JPEGs.
    #   2. run_detect_faces also embeds from the thumbnail (see
    #      _photo_thumb_path call), so a user-added face matches the
    #      embedding space of detector clusters — pulling a different
    #      crop resolution would drift the cosine similarity.
    #   3. ML worker is already happy with thumbnails; we inherit that.
    src = _photo_thumb_path(p)
    if src is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            "썸네일이 아직 생성되지 않았습니다 (색인 완료 후 다시 시도하세요)",
        )

    # --- Crop + embed -----------------------------------------------------
    try:
        from PIL import Image as _PIL, ImageOps
        _PIL.MAX_IMAGE_PIXELS = 64_000_000
        with _PIL.open(src) as im:
            im = ImageOps.exif_transpose(im).convert("RGB")
            iw, ih = im.size
            # Denormalise + clamp to image bounds.
            px = max(0, int(x * iw))
            py = max(0, int(y * ih))
            pw = max(2, min(iw - px, int(w * iw)))
            ph = max(2, min(ih - py, int(h * ih)))
            crop = im.crop((px, py, px + pw, py + ph))
            crop = crop.resize(
                (faces_mod.FACE_CROP_SIZE, faces_mod.FACE_CROP_SIZE),
                _PIL.BILINEAR,
            )
            face_bgr = np.asarray(crop)[..., ::-1].astype(np.uint8)
    except Exception as e:
        raise HTTPException(
            status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            f"이미지를 디코딩할 수 없습니다: {e}",
        )

    try:
        faces_mod._load_embedder()                # raises FileNotFoundError
    except FileNotFoundError:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "SFace 모델이 설치되지 않았습니다 (install-ml-models.sh 실행 필요)",
        )
    emb = faces_mod._embed_face(face_bgr)         # L2-normalised float32

    # --- Pick cluster -----------------------------------------------------
    suggested = False
    suggested_sim: float | None = None

    target: FaceCluster | None = None
    if body.cluster_id is not None:
        target = db.get(FaceCluster, body.cluster_id)
        if target is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "cluster_id not found")
    elif body.label is not None and body.label.strip():
        label = body.label.strip()
        target = db.execute(
            select(FaceCluster).where(FaceCluster.label == label)
        ).scalar_one_or_none()
        if target is None:
            target = FaceCluster(label=label, face_count=0)
            db.add(target)
            db.flush()
    else:
        # Auto-match by embedding similarity.
        best: tuple[float, FaceCluster] | None = None
        # Stream cluster ids; per-cluster centroid is mean of fp32 embeddings.
        clusters = db.execute(select(FaceCluster)).scalars().all()
        for c in clusters:
            face_rows = db.execute(
                select(PhotoFace.embedding).where(PhotoFace.cluster_id == c.id)
            ).all()
            if not face_rows:
                continue
            vecs = [faces_mod.unpack_embedding(r[0]) for r in face_rows if r[0]]
            if not vecs:
                continue
            centroid = np.mean(np.stack(vecs), axis=0)
            cn = float(np.linalg.norm(centroid))
            if cn < 1e-6:
                continue
            centroid /= cn
            sim = float(np.dot(emb, centroid))
            if best is None or sim > best[0]:
                best = (sim, c)
        if best is not None and best[0] >= _FACE_MATCH_SIM_THRESHOLD:
            target = best[1]
            suggested = True
            suggested_sim = best[0]
        else:
            # No good match → start a new unnamed cluster.
            target = FaceCluster(label=None, face_count=0)
            db.add(target)
            db.flush()

    # --- Insert PhotoFace row --------------------------------------------
    bbox_json = json.dumps([x, y, w, h])
    pf = PhotoFace(
        photo_id=p.id,
        cluster_id=target.id,
        bbox_json=bbox_json,
        embedding=faces_mod.pack_embedding(emb),
        confidence=1.0,                # user-drawn = explicit
        source="user",                 # protect from _clear_existing_faces
    )
    db.add(pf)
    target.face_count = (target.face_count or 0) + 1
    db.commit()

    return AddFaceOut(
        face_id=pf.id,
        cluster_id=target.id,
        cluster_label=target.label,
        suggested=suggested,
        suggested_similarity=suggested_sim,
    )


class FacePatchIn(BaseModel):
    # Find-or-create target cluster by label (most common path).
    # Empty string / None / whitespace-only → fresh unnamed cluster
    # (the "split this out, I'll name it later" case).
    label: str | None = None
    # Explicit cluster id — bypasses label. For "merge this face into
    # cluster #N" from a future cluster-picker UI.
    cluster_id: int | None = None


class FacePatchOut(BaseModel):
    face_id: int
    cluster_id: int
    cluster_label: str | None
    # Useful to the frontend for a quick "moved from X" toast.
    old_cluster_id: int | None


@router.patch("/faces/{face_id}", response_model=FacePatchOut)
def patch_face(
    face_id: int,
    body: FacePatchIn,
    db: Session = Depends(get_db),
) -> FacePatchOut:
    """Reassign a single PhotoFace to a different cluster.

    Use case: YuNet (and SFace embeddings) sometimes pull two
    similar-looking people — sisters, siblings, parent + child —
    into one cluster. The admin viewing a mislabeled face wants to
    split THIS face out without touching the other 50 in the
    cluster. PATCH /clusters/{id} renames the whole group; this
    operates on a single PhotoFace row.

    Target picking, in order:
      - body.cluster_id given → use it (admin chose an existing
        cluster from a future picker)
      - body.label given → find-or-create cluster by label (so
        typing the same name twice auto-merges into the existing
        same-named cluster, matching the PATCH /clusters rename
        semantics)
      - neither → spin up a fresh unnamed cluster (pure "split")

    Decrements the old cluster's face_count and bumps the new
    one's. Empty source cluster stays — admin removes via the
    cluster DELETE endpoint.
    """
    f = db.get(PhotoFace, face_id)
    if f is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    old_cluster_id = f.cluster_id

    target: FaceCluster | None = None
    if body.cluster_id is not None:
        target = db.get(FaceCluster, body.cluster_id)
        if target is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "cluster_id not found")
    elif body.label is not None and body.label.strip():
        label = body.label.strip()
        target = db.execute(
            select(FaceCluster).where(FaceCluster.label == label)
        ).scalar_one_or_none()
        if target is None:
            target = FaceCluster(label=label, face_count=0)
            db.add(target)
            db.flush()
    else:
        # Split with no name — fresh unnamed cluster. User names it
        # later via the existing ✎ rename flow on the new box.
        target = FaceCluster(label=None, face_count=0)
        db.add(target)
        db.flush()

    if target.id != old_cluster_id:
        f.cluster_id = target.id
        target.face_count = (target.face_count or 0) + 1
        if old_cluster_id is not None:
            old = db.get(FaceCluster, old_cluster_id)
            if old is not None and (old.face_count or 0) > 0:
                old.face_count = old.face_count - 1
        db.commit()

    return FacePatchOut(
        face_id=f.id,
        cluster_id=target.id,
        cluster_label=target.label,
        old_cluster_id=old_cluster_id,
    )


@router.delete("/faces/{face_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_face(face_id: int, db: Session = Depends(get_db)) -> None:
    """Remove a single detected face row.

    Use case: the YuNet detector occasionally picks up something that
    isn't a face (a clock, a frame on the wall, a pattern). The
    lightbox exposes this as a × on each face box so the admin can
    purge the false positive without re-running the whole face stage.

    Side effect: decrement the owning cluster's face_count if any.
    We do NOT auto-delete clusters whose face_count hits 0 — the
    admin keeps those visible for explicit cleanup via the cluster
    DELETE above, and they cost almost nothing to leave around.
    """
    f = db.get(PhotoFace, face_id)
    if f is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    if f.cluster_id is not None:
        c = db.get(FaceCluster, f.cluster_id)
        if c is not None and (c.face_count or 0) > 0:
            c.face_count = c.face_count - 1
    db.delete(f)
    db.commit()

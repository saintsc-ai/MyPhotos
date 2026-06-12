"""Per-job handlers for the ML worker.

Each handler receives an open DB session and the job payload. They are
registered under their `kind` string in the dispatcher.

  classify_objects   — YOLO  → auto-yolo tags
  classify_embedding — CLIP  → photo_embeddings + auto-clip tags via categories
  detect_faces       — YuNet + SFace → photo_faces + face_clusters
"""

from __future__ import annotations

import json
import logging
from typing import Any

import numpy as np
from sqlalchemy import func, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from ..config import get_settings
from ..models import (
    FaceCluster,
    Photo,
    PhotoAutoTag,
    PhotoEmbedding,
    PhotoFace,
    Tag,
)
from ..worker.thumbs import thumb_path
from . import clip as clip_mod
from . import faces as faces_mod
from . import yolo
from .categories import CATEGORIES, ClipCategory
from .labels_yolo import label_for

log = logging.getLogger(__name__)


# --- shared helpers --------------------------------------------------------


def _ensure_tag(db: Session, name: str, default_source: str) -> Tag:
    """Look up the shared Tag dictionary entry by name (case-insensitive)
    or create it. `default_source` is recorded on freshly created rows
    only — for the new model the link source lives on PhotoAutoTag /
    PhotoTag, so Tag.source is just a legacy seed hint."""
    name = name.strip()
    existing = db.execute(
        select(Tag).where(func.lower(Tag.name) == name.lower())
    ).scalar_one_or_none()
    if existing is not None:
        return existing
    t = Tag(name=name, source=default_source)
    db.add(t)
    db.flush()
    return t


def _replace_auto_tags(
    db: Session,
    photo_id: int,
    source: str,
    new_tag_names: list[str],
    confidences: list[float] | None = None,
) -> None:
    """Replace this photo's PhotoAutoTag rows for the given source.

    User tags (photo_tags) and OTHER ML sources are untouched — each ML
    stage owns only its own source rows. Same (photo, tag, source)
    triple is unique, so re-running classification on the same photo
    just refreshes the set in place.
    """
    from sqlalchemy import delete as _delete
    db.execute(
        _delete(PhotoAutoTag).where(
            PhotoAutoTag.photo_id == photo_id,
            PhotoAutoTag.source == source,
        )
    )
    db.flush()

    confs = confidences or [None] * len(new_tag_names)
    for name, conf in zip(new_tag_names, confs):
        if not name:
            continue
        tag = _ensure_tag(db, name, default_source=source)
        # Same (photo, tag, source) shouldn't collide because we just
        # deleted that set, but guard so duplicate names in the
        # incoming list (rare with normalized inputs) don't crash.
        exists = db.execute(
            select(PhotoAutoTag).where(
                PhotoAutoTag.photo_id == photo_id,
                PhotoAutoTag.tag_id == tag.id,
                PhotoAutoTag.source == source,
            )
        ).scalar_one_or_none()
        if exists is None:
            db.add(PhotoAutoTag(
                photo_id=photo_id, tag_id=tag.id,
                source=source, confidence=conf,
            ))


def _photo_thumb_path(photo: Photo) -> str | None:
    settings = get_settings()
    for sz in sorted(settings.thumbnails.sizes, reverse=True):
        p = thumb_path(photo.sha256, sz)
        if p.exists():
            return str(p)
    return None


# --- YOLO ------------------------------------------------------------------


def run_classify_objects(db: Session, payload: dict[str, Any]) -> None:
    photo_id = int(payload["photo_id"])
    p = db.get(Photo, photo_id)
    if p is None or not p.sha256:
        return
    src = _photo_thumb_path(p)
    if src is None:
        return  # no thumb yet; leave pending for retry

    detections = yolo.detect(src)
    if detections is None:
        return  # model missing — leave pending
    labels = [label_for(d.class_id) for d in detections]
    _replace_auto_tags(db, photo_id, source="auto-yolo", new_tag_names=labels)
    p.objects_status = "ok"
    db.commit()
    from .. import fts as _fts
    _fts.rebuild_photo(db, photo_id)
    db.commit()


# --- CLIP ------------------------------------------------------------------

# Cached text embeddings for the curated category list — computed once per
# process on first need.
_CAT_TEXT_VECS: np.ndarray | None = None


def _category_vectors() -> np.ndarray | None:
    global _CAT_TEXT_VECS
    if _CAT_TEXT_VECS is not None:
        return _CAT_TEXT_VECS
    prompts = [c.prompt for c in CATEGORIES]
    vecs = clip_mod.encode_text(prompts)
    if vecs is None:
        return None
    _CAT_TEXT_VECS = vecs
    log.info("CLIP: cached %d category text embeddings", len(prompts))
    return _CAT_TEXT_VECS


def _resolve_exclusive_groups(
    matches: list[str],
    scores: dict[str, float],
    groups: list[list[str]],
) -> list[str]:
    """Collapse mutually-exclusive category matches to one per group.

    For each group, if more than one of its members cleared its threshold,
    keep only the highest-scoring one and drop the others. Categories that
    belong to no group (or are the lone match in theirs) pass through
    unchanged, so non-exclusive labels stay multi-label. Order is preserved.
    """
    drop: set[str] = set()
    for group in groups:
        present = [n for n in matches if n in group]
        if len(present) > 1:
            winner = max(present, key=lambda n: scores.get(n, 0.0))
            drop.update(n for n in present if n != winner)
    if not drop:
        return matches
    return [n for n in matches if n not in drop]


def run_classify_embedding(db: Session, payload: dict[str, Any]) -> None:
    """Compute the photo's CLIP image embedding, store it, then auto-tag
    via cosine similarity against each curated category."""
    photo_id = int(payload["photo_id"])
    p = db.get(Photo, photo_id)
    if p is None or not p.sha256:
        return
    src = _photo_thumb_path(p)
    if src is None:
        return

    img_vec = clip_mod.encode_image(src)
    if img_vec is None:
        return  # model missing

    # Persist embedding.
    existing = db.get(PhotoEmbedding, photo_id)
    blob = clip_mod.pack_vector(img_vec)
    if existing is None:
        db.add(PhotoEmbedding(
            photo_id=photo_id, model="clip-vit-b32", vector=blob,
        ))
    else:
        existing.model = "clip-vit-b32"
        existing.vector = blob
    p.clip_status = "ok"   # embedding stored; category tags below are best-effort

    # Score against categories.
    cat_vecs = _category_vectors()
    if cat_vecs is None:
        db.commit()
        return
    sims = cat_vecs @ img_vec   # (N,)
    scores = {CATEGORIES[i].name: float(s) for i, s in enumerate(sims)}
    matches = [
        CATEGORIES[i].name
        for i, s in enumerate(sims)
        if s >= CATEGORIES[i].threshold
    ]
    groups = get_settings().ml.exclusive_category_groups
    if groups:
        matches = _resolve_exclusive_groups(matches, scores, groups)
    _replace_auto_tags(db, photo_id, source="auto-clip", new_tag_names=matches)
    db.commit()
    from .. import fts as _fts
    _fts.rebuild_photo(db, photo_id)
    db.commit()


# --- Faces -----------------------------------------------------------------


def _clear_existing_faces(db: Session, photo_id: int) -> None:
    """Drop any prior face rows for this photo (re-detect path). Decrement
    each affected cluster's face_count; orphan clusters with 0 count are
    left as-is so the admin can clean them up explicitly."""
    rows = db.execute(
        select(PhotoFace).where(PhotoFace.photo_id == photo_id)
    ).scalars().all()
    cluster_decrement: dict[int, int] = {}
    for f in rows:
        if f.cluster_id is not None:
            cluster_decrement[f.cluster_id] = cluster_decrement.get(f.cluster_id, 0) + 1
        db.delete(f)
    for cid, n in cluster_decrement.items():
        c = db.get(FaceCluster, cid)
        if c is not None:
            c.face_count = max(0, c.face_count - n)
    db.flush()


def run_detect_faces(db: Session, payload: dict[str, Any]) -> None:
    photo_id = int(payload["photo_id"])
    p = db.get(Photo, photo_id)
    if p is None or not p.sha256:
        return
    src = _photo_thumb_path(p)
    if src is None:
        return

    detections = faces_mod.detect_and_embed(src)
    if detections is None:
        return  # models missing — leave pending

    _clear_existing_faces(db, photo_id)

    for d in detections:
        cluster = faces_mod.assign_or_create_cluster(db, d["embedding"])
        db.add(PhotoFace(
            photo_id=photo_id,
            bbox_json=json.dumps(d["bbox"]),
            embedding=faces_mod.pack_embedding(d["embedding"]),
            cluster_id=cluster.id,
            confidence=float(d["score"]),
        ))
    p.faces_status = "ok"
    db.commit()


def run_ocr_text(db: Session, payload: dict[str, Any]) -> None:
    """OCR the photo's thumbnail and store the text (feeds FTS search).

    Images only. Engine-unavailable (rapidocr not installed) leaves the
    job pending so it auto-resumes after the package is installed —
    mirroring the model-missing behaviour of the classify handlers.
    """
    photo_id = int(payload["photo_id"])
    p = db.get(Photo, photo_id)
    if p is None or not p.sha256:
        return
    if p.media_kind != "image":
        _ocr_persist(db, photo_id, None, "skipped")
        return
    src = _photo_thumb_path(p)
    if src is None:
        return  # no thumb yet; leave pending for retry

    from . import ocr as ocr_mod
    try:
        text = ocr_mod.extract_text(src)
    except Exception as e:  # per-image OCR error — mark failed, don't loop
        log.warning("ocr_text: photo %d failed: %s", photo_id, e)
        _ocr_persist(db, photo_id, None, "failed")
        return
    if text is None:
        return  # engine unavailable — leave pending

    _ocr_persist(db, photo_id, text or None, "ok" if text else "empty")


def _ocr_persist(db: Session, photo_id: int, ocr_text, status: str) -> None:
    """Write the OCR result, retrying on SQLite 'database is locked' (heavy
    OCR/ML batches starve the single writer past busy_timeout). Re-fetches
    the row each attempt since a failed commit rolls back. FTS is rebuilt
    only when text was found ('ok')."""
    import time as _time

    from .. import fts as _fts

    for attempt in range(6):
        try:
            p = db.get(Photo, photo_id)
            if p is None:
                return
            p.ocr_text = ocr_text
            p.ocr_status = status
            db.commit()
            if status == "ok":
                _fts.rebuild_photo(db, photo_id)
                db.commit()
            return
        except OperationalError as e:
            db.rollback()
            if "locked" in str(e).lower() and attempt < 5:
                _time.sleep(0.5 * (attempt + 1))
                continue
            raise


def run_recluster_faces(db: Session, payload: dict[str, Any]) -> None:
    """Rebuild all face clusters from scratch (admin-triggered). Thresholds
    optional in payload; defaults live in faces.py."""
    kw = {}
    for k in ("join_threshold", "merge_threshold", "min_face_frac"):
        if payload.get(k) is not None:
            kw[k] = float(payload[k])
    res = faces_mod.recluster_all(db, **kw)
    log.info("recluster_faces done: %s", res)


def _set_photo_status(db: Session, photo_id: int, col: str, value: str) -> None:
    """Set one Photo status column, retrying on a transient SQLite lock."""
    import time as _t
    for i in range(6):
        try:
            p = db.get(Photo, photo_id)
            if p is None:
                return
            setattr(p, col, value)
            db.commit()
            return
        except OperationalError as e:
            db.rollback()
            if "locked" in str(e).lower() and i < 5:
                _t.sleep(0.4 * (i + 1))
                continue
            raise


def _rollup_classify_status(db: Session, photo_id: int) -> None:
    """Maintain the legacy classify_status from the three per-stage columns:
    ok when all are ok/skipped; failed when any failed and none still pending;
    pending otherwise."""
    for i in range(6):
        try:
            p = db.get(Photo, photo_id)
            if p is None:
                return
            sub = (p.objects_status, p.clip_status, p.faces_status)
            if all(s in ("ok", "skipped") for s in sub):
                rolled = "ok"
            elif any(s == "failed" for s in sub) and not any(s == "pending" for s in sub):
                rolled = "failed"
            else:
                rolled = "pending"
            if p.classify_status != rolled:
                p.classify_status = rolled
                db.commit()
            return
        except OperationalError as e:
            db.rollback()
            if "locked" in str(e).lower() and i < 5:
                import time as _t
                _t.sleep(0.4 * (i + 1))
                continue
            raise


def run_classify_ml(db: Session, payload: dict[str, Any]) -> None:
    """Unified per-photo ML job. The image is the key; each ML stage has its
    own status column (objects/clip/faces + ocr). Run only the stages still
    pending and skip the done ones — one queue row per photo, no per-kind
    starvation, 4× fewer rows.

    Each stage is isolated: a per-image error fails only that stage's column
    (the others still run and complete), so a later pass retries just what's
    left. A transient DB lock re-raises so the dispatcher requeues the whole
    job. classify_status is rolled up afterwards for back-compat.
    """
    photo_id = int(payload["photo_id"])
    p = db.get(Photo, photo_id)
    if p is None or not p.sha256:
        return

    stages = (
        ("objects_status", run_classify_objects),
        ("clip_status", run_classify_embedding),
        ("faces_status", run_detect_faces),
    )
    for col, fn in stages:
        cur = db.get(Photo, photo_id)
        if cur is None or getattr(cur, col) in ("ok", "skipped"):
            continue
        try:
            fn(db, payload)        # sets its own column to 'ok' on success
        except Exception as e:
            db.rollback()
            if isinstance(e, OperationalError) and "locked" in str(e).lower():
                raise              # requeue whole job; redo only-pending next pass
            log.exception("ml stage %s failed for photo %d", col, photo_id)
            _set_photo_status(db, photo_id, col, "failed")

    # OCR — images only, own axis (ocr_status).
    cur = db.get(Photo, photo_id)
    if cur is not None and cur.media_kind == "image" \
            and cur.ocr_status not in ("ok", "empty", "skipped"):
        try:
            run_ocr_text(db, payload)
        except Exception as e:
            db.rollback()
            if isinstance(e, OperationalError) and "locked" in str(e).lower():
                raise
            log.exception("ml stage ocr failed for photo %d", photo_id)
            _set_photo_status(db, photo_id, "ocr_status", "failed")

    _rollup_classify_status(db, photo_id)


HANDLERS = {
    # Unified per-photo job (current path). The four single-stage handlers
    # below stay registered so any jobs queued by an older build still drain.
    "classify_ml": run_classify_ml,
    "classify_objects": run_classify_objects,
    "classify_embedding": run_classify_embedding,
    "detect_faces": run_detect_faces,
    "ocr_text": run_ocr_text,
    "recluster_faces": run_recluster_faces,
}

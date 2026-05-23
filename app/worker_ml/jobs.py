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
from sqlalchemy.orm import Session

from ..config import get_settings
from ..models import (
    FaceCluster,
    Photo,
    PhotoEmbedding,
    PhotoFace,
    PhotoTag,
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


def _ensure_auto_tag(db: Session, name: str, source: str) -> Tag:
    name = name.strip()
    existing = db.execute(
        select(Tag).where(func.lower(Tag.name) == name.lower())
    ).scalar_one_or_none()
    if existing is not None:
        return existing
    t = Tag(name=name, source=source)
    db.add(t)
    db.flush()
    return t


def _replace_auto_tags(
    db: Session,
    photo_id: int,
    source: str,
    new_tag_names: list[str],
) -> None:
    """Drop existing photo_tags for this photo whose tag.source == source,
    then add fresh ones. Leaves user tags + other sources untouched."""
    old_links = db.execute(
        select(PhotoTag).join(Tag, Tag.id == PhotoTag.tag_id).where(
            PhotoTag.photo_id == photo_id,
            Tag.source == source,
        )
    ).scalars().all()
    for link in old_links:
        db.delete(link)
    db.flush()

    for name in new_tag_names:
        if not name:
            continue
        tag = _ensure_auto_tag(db, name, source)
        exists = db.execute(
            select(PhotoTag).where(
                PhotoTag.photo_id == photo_id, PhotoTag.tag_id == tag.id
            )
        ).scalar_one_or_none()
        if exists is None:
            db.add(PhotoTag(photo_id=photo_id, tag_id=tag.id))


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
    p.classify_status = "ok"
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

    # Score against categories.
    cat_vecs = _category_vectors()
    if cat_vecs is None:
        db.commit()
        return
    sims = cat_vecs @ img_vec   # (N,)
    matches = [
        CATEGORIES[i].name
        for i, s in enumerate(sims)
        if s >= CATEGORIES[i].threshold
    ]
    _replace_auto_tags(db, photo_id, source="auto-clip", new_tag_names=matches)
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
    db.commit()


HANDLERS = {
    "classify_objects": run_classify_objects,
    "classify_embedding": run_classify_embedding,
    "detect_faces": run_detect_faces,
}

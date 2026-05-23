"""Per-job handlers for the ML worker.

Each handler receives an open DB session and the job payload. They are
registered in the ML dispatcher under their `kind` string. Round 1 only
implements `classify_objects` (YOLO); `classify_embedding` (CLIP) and
`detect_faces` are scaffolded so the dispatcher already knows about
them — they raise NotImplementedError until later rounds.
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..models import Photo, PhotoTag, Tag
from ..worker.thumbs import thumb_path
from . import yolo
from .labels_yolo import label_for

log = logging.getLogger(__name__)


def _ensure_auto_tag(db: Session, name: str, source: str) -> Tag:
    """Find-or-create a tag with the given source. Case-insensitive match
    on existing names so we don't end up with parallel "사람" / "사람 " rows."""
    name = name.strip()
    from sqlalchemy import func
    existing = db.execute(
        select(Tag).where(func.lower(Tag.name) == name.lower())
    ).scalar_one_or_none()
    if existing is not None:
        # If a user happened to make a tag with the same name first, leave it
        # alone (don't flip its source).
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
    """Remove all photo_tags for this photo whose tag.source == source, then
    add the new ones. Leaves user tags + other sources untouched."""
    # Drop the existing auto-* tag links for this source.
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
        # Only add the link if it doesn't already exist (in case the same
        # name was somehow both a user tag and a YOLO label).
        exists = db.execute(
            select(PhotoTag).where(
                PhotoTag.photo_id == photo_id, PhotoTag.tag_id == tag.id
            )
        ).scalar_one_or_none()
        if exists is None:
            db.add(PhotoTag(photo_id=photo_id, tag_id=tag.id))


def run_classify_objects(db: Session, payload: dict[str, Any]) -> None:
    """Run YOLO on the photo's 1024 thumbnail and write `auto-yolo` tags."""
    photo_id = int(payload["photo_id"])
    p = db.get(Photo, photo_id)
    if p is None or not p.sha256:
        log.info("classify_objects: photo %s not indexable, skipping", photo_id)
        return

    settings = get_settings()
    # Prefer the largest available thumb; fall back through smaller sizes.
    sizes = sorted(settings.thumbnails.sizes, reverse=True)
    src = None
    for sz in sizes:
        candidate = thumb_path(p.sha256, sz)
        if candidate.exists():
            src = candidate
            break
    if src is None:
        log.info("classify_objects: no thumb for photo %s yet", photo_id)
        # Don't mark failed — thumb might still be generating.
        return

    detections = yolo.detect(str(src))
    if detections is None:
        # Model missing — leave classify_status as 'pending' so a later
        # retry after install picks it up.
        return

    labels = [label_for(d.class_id) for d in detections]
    _replace_auto_tags(db, photo_id, source="auto-yolo", new_tag_names=labels)
    p.classify_status = "ok"
    db.commit()


def run_classify_embedding(db: Session, payload: dict[str, Any]) -> None:  # Round 2
    raise NotImplementedError("CLIP embeddings — Round 2")


def run_detect_faces(db: Session, payload: dict[str, Any]) -> None:  # Round 3
    raise NotImplementedError("Face detection — Round 3")


HANDLERS = {
    "classify_objects": run_classify_objects,
    "classify_embedding": run_classify_embedding,
    "detect_faces": run_detect_faces,
}

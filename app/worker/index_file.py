"""Handler for the 'index_file' job kind.

Stages, in order:
  1. Verify the file still exists at its recorded path.
  2. Compute SHA-256 (streamed).
  3. Extract EXIF (Pillow -> ExifTool fallback chain).
  4. Generate thumbnails (sizes from config).
  5. Persist GPS to photo_locations if present.

Each stage commits independently so partial failures don't lose work.
"""

from __future__ import annotations

import hashlib
import logging
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Photo, PhotoLocation, Root, User
from ..scanner.utils import join_root
from . import exif as exif_mod
from . import thumbs as thumb_mod

log = logging.getLogger(__name__)

_HASH_CHUNK = 1024 * 1024  # 1 MiB


def _sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(_HASH_CHUNK)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def _dedup_actor(db: Session, photo: Photo) -> User | None:
    """Pick a User to attribute the auto-trash to: the photo's own uploader
    if known, otherwise the lowest-id admin (any user as a last resort)."""
    if photo.owner_user_id:
        u = db.get(User, photo.owner_user_id)
        if u is not None:
            return u
    u = db.execute(
        select(User).where(User.is_admin.is_(True)).order_by(User.id).limit(1)
    ).scalar_one_or_none()
    if u is not None:
        return u
    return db.execute(select(User).order_by(User.id).limit(1)).scalar_one_or_none()


def _trash_if_duplicate(db: Session, photo: Photo) -> bool:
    """If an ACTIVE photo with a smaller id already has this sha256, trash
    `photo` (the incoming duplicate) and return True. Keeping the lowest id
    makes the choice deterministic under concurrent indexing — the earliest
    copy survives; any later copy trashes itself."""
    dup = db.execute(
        select(Photo.id).where(
            Photo.sha256 == photo.sha256,
            Photo.status == "active",
            Photo.id < photo.id,
        ).limit(1)
    ).first()
    if dup is None:
        return False
    actor = _dedup_actor(db, photo)
    if actor is None:
        return False  # no users at all — nothing to attribute to; leave it
    from ..api.routes_photos import trash_photos_core
    res = trash_photos_core(db, [photo.id], actor, bulk=True)
    if photo.id in res.get("ids", []):
        log.info("index_file: photo %d is a duplicate of %d → trashed",
                 photo.id, dup[0])
        return True
    # Couldn't trash (e.g. readonly root) — leave it active; the manual
    # 중복 제거 sweep can still handle it later.
    log.info("index_file: photo %d duplicate of %d but not trashed (%s)",
             photo.id, dup[0], res.get("skipped_readonly") or res.get("failed"))
    return False


def run(db: Session, payload: dict[str, Any]) -> None:
    photo_id = int(payload["photo_id"])
    photo = db.get(Photo, photo_id)
    if photo is None:
        log.warning("index_file: photo %d not found, skipping", photo_id)
        return
    # Trashed photos legitimately don't exist at root.abs_path/rel_path —
    # their file was moved to data/trash/<id>/. A retry-photos sweep
    # that doesn't filter on status='active' would otherwise enqueue
    # index_file jobs for them, then this handler would see the file
    # absent and overwrite status to 'missing' (losing the trash link)
    # — silent data corruption that's caught only when the user tries
    # to restore from trash and finds the photo is no longer there.
    # Bail out cleanly instead.
    if photo.status == "trashed":
        log.info("index_file: photo %d is trashed, skipping (file lives in data/trash/)", photo_id)
        return
    root = db.get(Root, photo.root_id)
    if root is None:
        log.warning("index_file: root %d not found for photo %d", photo.root_id, photo_id)
        return

    abs_path = join_root(root.abs_path, photo.rel_path)

    # 1. existence
    import os

    if not os.path.exists(abs_path):
        photo.status = "missing"
        db.commit()
        log.info("index_file: %s no longer exists, marked missing", abs_path)
        return

    # 2. hash + EXIF (single commit at the end of this stage)
    stage1_dirty = False
    if not photo.sha256:
        try:
            photo.sha256 = _sha256_file(abs_path)
            stage1_dirty = True
        except OSError as e:
            log.warning("index_file: hash failed for %s: %s", abs_path, e)
            return
        # Ingest dedup: a file that didn't pass through /upload (e.g.
        # PhotoSync over SMB) can be a content duplicate of one already in
        # the catalog. The upload endpoint blocks those up-front; here we
        # do the equivalent at index time. Keep the lowest id (the earliest
        # copy), trash the incoming one. Opt-in via [dedup] skip_ingest.
        from ..config import get_settings
        if get_settings().dedup.skip_ingest:
            db.commit()           # persist sha so the dup query is consistent
            stage1_dirty = False
            if _trash_if_duplicate(db, photo):
                return

    if photo.exif_status == "pending":
        r = exif_mod.extract(abs_path, media_kind=photo.media_kind)
        photo.taken_at = r.taken_at or photo.taken_at
        photo.width = r.width or photo.width
        photo.height = r.height or photo.height
        photo.camera_make = r.camera_make or photo.camera_make
        photo.camera_model = r.camera_model or photo.camera_model
        photo.lens = r.lens or photo.lens
        photo.iso = r.iso or photo.iso
        photo.fnumber = r.fnumber or photo.fnumber
        photo.exposure = r.exposure or photo.exposure
        photo.focal_length = r.focal_length or photo.focal_length
        photo.orientation = r.orientation or photo.orientation
        photo.duration_seconds = r.duration_seconds or photo.duration_seconds
        photo.exif_status = r.status
        photo.exif_extractor = r.extractor
        photo.exif_error = r.error

        # GPS — guard the photo_locations CHECK constraint here too in
        # case a future extractor forgets to sanitize its output.
        # isinstance/finite checks catch the "empty string slipped
        # through sanitize" path that produced
        # `could not convert string to float: ''` on real-world data.
        import math
        lat_ok = (
            isinstance(r.latitude, (int, float))
            and not isinstance(r.latitude, bool)
            and math.isfinite(r.latitude)
            and -90.0 <= r.latitude <= 90.0
        )
        lng_ok = (
            isinstance(r.longitude, (int, float))
            and not isinstance(r.longitude, bool)
            and math.isfinite(r.longitude)
            and -180.0 <= r.longitude <= 180.0
        )
        if lat_ok and lng_ok and not (r.latitude == 0.0 and r.longitude == 0.0):
            # Altitude is optional and float-or-None; force the same shape.
            alt = r.altitude
            if isinstance(alt, str):
                alt = alt.strip()
                if not alt:
                    alt = None
                else:
                    try:
                        alt = float(alt)
                    except ValueError:
                        alt = None
            if alt is not None and (
                not isinstance(alt, (int, float))
                or isinstance(alt, bool)
                or not math.isfinite(alt)
            ):
                alt = None

            loc = db.get(PhotoLocation, photo.id)
            if loc is None:
                loc = PhotoLocation(
                    photo_id=photo.id,
                    latitude=float(r.latitude),
                    longitude=float(r.longitude),
                    altitude=alt,
                )
                db.add(loc)
            else:
                loc.latitude = float(r.latitude)
                loc.longitude = float(r.longitude)
                loc.altitude = alt
        stage1_dirty = True

    if stage1_dirty:
        db.commit()

    # 3. thumbnails — kept as a separate commit so a thumb failure doesn't
    # undo EXIF progress (e.g. when exiftool succeeds but ffmpeg is missing).
    if photo.thumb_status == "pending" and photo.sha256:
        tr = thumb_mod.generate(abs_path, photo.sha256, media_kind=photo.media_kind)
        photo.thumb_status = tr.status
        photo.thumb_error = tr.error
        db.commit()

    # 4. auto-queue ML + OCR (opt-in). Mirrors the manual 관리 → ML 자동 분류
    #    run but per-photo as files arrive — only once thumbs exist and the
    #    stage hasn't run yet, so re-index passes don't pile up duplicates.
    if photo.thumb_status in ("ok", "partial"):
        _maybe_auto_enqueue(db, photo)
    # 5. auto-queue GPS estimation when the photo has a usable taken_at
    #    but no location. EXIF + taken_at are settled by this point in
    #    the pipeline, so estimation against folder/parent-folder
    #    anchors can run as soon as the file lands — no need to wait
    #    for an admin to push a button.
    _maybe_auto_enqueue_location(db, photo)


def _maybe_auto_enqueue_location(db: Session, photo: Photo) -> None:
    """Mark the estimate_location stage pending on this photo's
    photo_work row. The new dispatcher walks the stage in order
    after index + classify; nothing to do if there's already a real
    EXIF/user location.
    """
    if photo.taken_at is None:
        return
    if photo.exif_status not in ("ok", "partial"):
        return
    existing = db.get(PhotoLocation, photo.id)
    if existing is not None and existing.source in ("exif", "user"):
        return
    from . import photo_work as photo_work_mod
    photo_work_mod.enqueue_stage(
        db, photo_id=photo.id, stage="estimate_location", priority=0,
    )
    db.commit()


def _maybe_auto_enqueue(db: Session, photo: Photo) -> None:
    from ..config import get_settings

    if not get_settings().ml.auto_enqueue:
        return
    from . import jobs as jobs_mod

    # One unified ML job per photo handles every stage that isn't done yet
    # (objects + CLIP + faces share classify_status; OCR is images-only on
    # ocr_status). 'pending' classify or never-attempted OCR ⇒ work to do.
    need_classify = "pending" in (
        photo.objects_status, photo.clip_status, photo.faces_status)
    need_ocr = photo.media_kind == "image" and photo.ocr_status is None
    if not (need_classify or need_ocr):
        return
    # Mark OCR queued so a later re-index pass doesn't enqueue a duplicate.
    if need_ocr:
        photo.ocr_status = "pending"
    # enqueue_unique_for_photo: if an earlier index_file pass (or admin
    # request) already left a classify_ml job in queued/running for this
    # photo, don't add another — the existing job re-reads photo.*_status
    # on pickup so the newly-toggled OCR stage lands on it automatically.
    # Recency boost on top of the base priority so a today's photo's ML
    # work clears ahead of the archive backlog.
    _prio = 4 + jobs_mod.recency_priority_boost(photo.mtime)
    # photo_work routes classify through the legacy classify_ml job
    # (ml-worker process boundary) — handler details in
    # photo_work._classify_handler. Mark the stage pending here; the
    # dispatcher will hand off to ml-worker when it reaches the row.
    from . import photo_work as photo_work_mod
    photo_work_mod.enqueue_stage(
        db, photo_id=photo.id, stage="classify", priority=_prio,
    )
    db.commit()

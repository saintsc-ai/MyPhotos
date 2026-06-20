"""Admin: unified indexing stage matrix.

Single endpoint that powers the new "indexing progress" admin page —
returns one consistent snapshot of every per-photo pipeline stage,
so the UI can render a stage-by-stage matrix (total / pending /
running / done / failed) without juggling 4-5 fragmented endpoints.

Stages exposed:
    exif         EXIF extraction              applies to all photos
    thumb        Thumbnail generation         applies to all photos
    pair         Live photo (HEIC↔MOV) pair   special — count only
    geo_estimate GPS inference from neighbours  taken_at + no real GPS
    ml_objects   YOLO object detection          images
    ml_clip      CLIP embedding                 images
    ml_faces     Face detection + clustering    images
    ocr          OCR text                       images (opt-in)
    transcode    H.264 web proxy                videos

"running" counts photo_work rows that are currently claimed by a
worker AND have this stage marked pending — i.e. "a worker is doing
this stage on a photo right now". Legacy `jobs` running is not
folded in (see scope below).
"""

from __future__ import annotations

import json
import logging
from typing import Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..api.deps import get_db
from ..models import Photo, PhotoLocation, PhotoWork

router = APIRouter(prefix="/admin/indexing", tags=["admin", "indexing"])
log = logging.getLogger(__name__)


# ---------- response models -----------------------------------------


class StageRow(BaseModel):
    key: str                       # stable id used by the trigger endpoint
    label_key: str                 # i18n key the frontend resolves
    applicable_total: int          # photos this stage applies to
    pending: int                   # status=pending (or NULL on opt-in stages)
    running: int                   # claimed in photo_work right now
    done: int                      # status=ok (+ partial / empty etc., see notes)
    failed: int                    # status=failed
    skipped: int                   # status=skipped (stage not applicable to this row)
    extra: dict                    # stage-specific extras (paired_count, …)


class IndexingScope(BaseModel):
    total_photos: int
    total_images: int
    total_videos: int
    total_with_taken_at: int


class StageMatrix(BaseModel):
    scope: IndexingScope
    stages: list[StageRow]


# ---------- helpers --------------------------------------------------


def _status_counts(
    db: Session, column, where_extra=None
) -> dict[str, int]:
    """COUNT(*) GROUP BY <status column>. Returns dict with raw status
    strings (incl. NULL → 'null'). Caller maps them into the
    pending/done/failed/skipped buckets."""
    q = select(column, func.count(Photo.id)).where(Photo.status == "active")
    if where_extra is not None:
        q = q.where(where_extra)
    q = q.group_by(column)
    out: dict[str, int] = {}
    for val, n in db.execute(q).all():
        key = "null" if val is None else str(val)
        out[key] = int(n)
    return out


def _bucket(counts: dict[str, int], done_extra=()) -> dict[str, int]:
    """Common pending/running/done/failed/skipped bucketing for the
    pipeline stages whose status column uses the standard vocabulary.

    `done_extra` lets a stage roll extra status values into 'done'
    (e.g. exif has 'partial', ocr has 'empty', which are successes
    that aren't literally 'ok')."""
    return {
        "pending": counts.get("pending", 0) + counts.get("null", 0),
        "done": counts.get("ok", 0) + sum(counts.get(k, 0) for k in done_extra),
        "failed": counts.get("failed", 0),
        "skipped": counts.get("skipped", 0),
    }


def _claimed_by_stage(db: Session) -> dict[str, int]:
    """Count photo_work rows claimed by a worker right now, broken
    down by which stage the worker is on. A claimed row's stages JSON
    has exactly one stage at 'pending' at the moment the dispatcher
    grabbed it (later stages haven't started yet) — we report that as
    the 'running' stage for matrix purposes."""
    rows = db.execute(
        select(PhotoWork.stages).where(PhotoWork.claim_token.is_not(None))
    ).all()
    counts: dict[str, int] = {}
    for (stages_json,) in rows:
        try:
            stages = json.loads(stages_json or "{}")
        except (ValueError, TypeError):
            continue
        for name, state in stages.items():
            if state == "pending":
                counts[name] = counts.get(name, 0) + 1
                # First pending wins — the dispatcher only runs one
                # stage at a time per row.
                break
    return counts


# Map matrix-row keys → which photo_work stage names contribute to
# their "running" count. Several matrix rows (the 4 ML rows) share a
# single photo_work stage (`classify`) because the ml-worker handles
# all 4 substages inside one classify_ml job.
_RUNNING_STAGE_MAP: dict[str, list[str]] = {
    "exif":         ["index"],
    "thumb":        ["index"],
    "pair":         [],
    "geo_estimate": ["estimate_location"],
    "ml_objects":   ["classify"],
    "ml_clip":      ["classify"],
    "ml_faces":     ["classify"],
    "ocr":          ["classify"],
    "transcode":    ["transcode"],
}


# ---------- endpoint -------------------------------------------------


@router.get("/stage-matrix", response_model=StageMatrix)
def stage_matrix(db: Session = Depends(get_db)) -> StageMatrix:
    """One snapshot of every per-photo pipeline stage. Designed to
    drive a stage-by-stage progress table — frontend just maps the
    stages array to rows.

    All counts scope to status='active' photos (trashed/missing rows
    don't show up in any pipeline view)."""
    # ---- scope ----
    active = (Photo.status == "active")
    total_photos = int(db.execute(
        select(func.count(Photo.id)).where(active)
    ).scalar() or 0)
    total_images = int(db.execute(
        select(func.count(Photo.id)).where(active, Photo.media_kind == "image")
    ).scalar() or 0)
    total_videos = int(db.execute(
        select(func.count(Photo.id)).where(active, Photo.media_kind == "video")
    ).scalar() or 0)
    total_with_taken_at = int(db.execute(
        select(func.count(Photo.id)).where(active, Photo.taken_at.is_not(None))
    ).scalar() or 0)

    # ---- claimed-now lookup (one query, reused across stages) ----
    claimed = _claimed_by_stage(db)
    def _running(row_key: str) -> int:
        return sum(claimed.get(s, 0) for s in _RUNNING_STAGE_MAP.get(row_key, []))

    # ---- per-stage status counts ----
    images_only = Photo.media_kind == "image"
    videos_only = Photo.media_kind == "video"

    exif_b   = _bucket(_status_counts(db, Photo.exif_status), done_extra=("partial",))
    thumb_b  = _bucket(_status_counts(db, Photo.thumb_status), done_extra=("partial",))
    obj_b    = _bucket(_status_counts(db, Photo.objects_status, images_only))
    clip_b   = _bucket(_status_counts(db, Photo.clip_status, images_only))
    face_b   = _bucket(_status_counts(db, Photo.faces_status, images_only))
    ocr_b    = _bucket(_status_counts(db, Photo.ocr_status, images_only),
                       done_extra=("empty",))
    proxy_b  = _bucket(_status_counts(db, Photo.proxy_status, videos_only))

    # ---- live photo pairing — special-case ----
    paired = int(db.execute(
        select(func.count(Photo.id)).where(
            active, Photo.companion_id.is_not(None),
        )
    ).scalar() or 0)
    # Candidates: photos whose extension *could* be one half of a
    # live pair (HEIC/JPG image OR MOV video). The pair_companions
    # job only considers these; everything else is "n/a", not "missing
    # pair".
    pair_candidates = int(db.execute(
        select(func.count(Photo.id)).where(active).where(
            (Photo.media_kind == "image")
            | ((Photo.media_kind == "video")
               & (func.lower(Photo.rel_path).like("%.mov")))
        )
    ).scalar() or 0)

    # ---- geo estimation — applicable = photos with taken_at and no
    #      real (EXIF/user) location, since the estimator never
    #      overrides those. "done" = currently has an estimated
    #      location row.
    no_real_loc_subq = (
        select(PhotoLocation.photo_id).where(
            PhotoLocation.source.in_(("exif", "user"))
        )
    )
    geo_applicable = int(db.execute(
        select(func.count(Photo.id)).where(
            active,
            Photo.taken_at.is_not(None),
            ~Photo.id.in_(no_real_loc_subq),
        )
    ).scalar() or 0)
    geo_done = int(db.execute(
        select(func.count(PhotoLocation.photo_id))
        .join(Photo, Photo.id == PhotoLocation.photo_id)
        .where(active, PhotoLocation.source == "estimated")
    ).scalar() or 0)

    stages: list[StageRow] = [
        StageRow(
            key="exif", label_key="indexing.stage_exif",
            applicable_total=total_photos,
            pending=exif_b["pending"], running=_running("exif"),
            done=exif_b["done"], failed=exif_b["failed"],
            skipped=exif_b["skipped"], extra={},
        ),
        StageRow(
            key="thumb", label_key="indexing.stage_thumb",
            applicable_total=total_photos,
            pending=thumb_b["pending"], running=_running("thumb"),
            done=thumb_b["done"], failed=thumb_b["failed"],
            skipped=thumb_b["skipped"], extra={},
        ),
        StageRow(
            key="pair", label_key="indexing.stage_pair",
            # Pairing produces N/2 pairs; UI shows pair_count vs the
            # candidate pool. applicable_total uses pair_candidates so
            # the "progress %" the frontend computes reads as
            # "fraction of pair-able photos that found a partner".
            applicable_total=pair_candidates,
            pending=max(0, pair_candidates - paired),
            running=0,           # pair_companions is one bulk admin job, no per-photo claim
            done=paired,
            failed=0,
            skipped=max(0, total_photos - pair_candidates),
            extra={"pair_count": paired // 2},
        ),
        StageRow(
            key="geo_estimate", label_key="indexing.stage_geo_estimate",
            applicable_total=geo_applicable,
            pending=max(0, geo_applicable - geo_done),
            running=_running("geo_estimate"),
            done=geo_done,
            failed=0,            # the estimator returns None silently on no anchor
            skipped=max(0, total_photos - geo_applicable),
            extra={},
        ),
        StageRow(
            key="ml_objects", label_key="indexing.stage_ml_objects",
            applicable_total=total_images,
            pending=obj_b["pending"], running=_running("ml_objects"),
            done=obj_b["done"], failed=obj_b["failed"],
            skipped=obj_b["skipped"], extra={},
        ),
        StageRow(
            key="ml_clip", label_key="indexing.stage_ml_clip",
            applicable_total=total_images,
            pending=clip_b["pending"], running=_running("ml_clip"),
            done=clip_b["done"], failed=clip_b["failed"],
            skipped=clip_b["skipped"], extra={},
        ),
        StageRow(
            key="ml_faces", label_key="indexing.stage_ml_faces",
            applicable_total=total_images,
            pending=face_b["pending"], running=_running("ml_faces"),
            done=face_b["done"], failed=face_b["failed"],
            skipped=face_b["skipped"], extra={},
        ),
        StageRow(
            key="ocr", label_key="indexing.stage_ocr",
            applicable_total=total_images,
            # OCR is opt-in — NULL means "never enqueued", which the
            # UI should treat as pending so the progress bar starts at
            # 0% not 100%. _bucket() already folds 'null' → pending.
            pending=ocr_b["pending"], running=_running("ocr"),
            done=ocr_b["done"], failed=ocr_b["failed"],
            skipped=ocr_b["skipped"], extra={},
        ),
        StageRow(
            key="transcode", label_key="indexing.stage_transcode",
            applicable_total=total_videos,
            pending=proxy_b["pending"], running=_running("transcode"),
            done=proxy_b["done"], failed=proxy_b["failed"],
            skipped=proxy_b["skipped"], extra={},
        ),
    ]

    return StageMatrix(
        scope=IndexingScope(
            total_photos=total_photos,
            total_images=total_images,
            total_videos=total_videos,
            total_with_taken_at=total_with_taken_at,
        ),
        stages=stages,
    )

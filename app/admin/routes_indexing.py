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
from ..worker import jobs as jobs_mod

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
    # YOLO / CLIP / faces all live inside the same classify_ml job, so
    # their status columns move in lockstep — we collapse them into
    # a single "ml" row that reads from Photo.classify_status (the
    # existing rollup) for the per-bucket counts. Power users who
    # want to retry just one substage still have the legacy ML card.
    "ml":           ["classify"],
    "ocr":          ["classify"],
    # transcode "running" is read directly from proxy_status='running'
    # (the handler stamps it in the DB before generate_proxy runs).
    # Counting both that AND photo_work claimed would double up since
    # they reflect the same moment in time.
    "transcode":    [],
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
    # ML uses the existing classify_status rollup ('ok' iff every
    # substage is ok/skipped; 'failed' if any substage failed; etc.)
    # — same source the old donut uses, so the numbers match.
    ml_b     = _bucket(_status_counts(db, Photo.classify_status, images_only))
    ocr_b    = _bucket(_status_counts(db, Photo.ocr_status, images_only),
                       done_extra=("empty",))

    # Transcode is the odd-stage out:
    #   - status vocab is 'pending' / 'running' / 'done' / 'failed' (not 'ok')
    #   - NULL means "playable as-is, no transcode needed" — counts as
    #     skipped, NOT as pending (mp4/MOV that the browser plays
    #     natively never enters the transcode pipeline)
    #   - 'running' is the DB-side truth (proxy_handler stamps it
    #     before generate_proxy runs), so don't double-count
    #     photo_work claimed.
    proxy_counts = _status_counts(db, Photo.proxy_status, videos_only)
    proxy_done    = proxy_counts.get("done", 0)
    proxy_pending = proxy_counts.get("pending", 0)
    proxy_running = proxy_counts.get("running", 0)
    proxy_failed  = proxy_counts.get("failed", 0)
    proxy_null    = proxy_counts.get("null", 0)
    # Applicable = videos that EVER entered the transcode queue (anything
    # not NULL). videos_to_transcode is what the progress bar should
    # divide by — otherwise we'd be measuring "fraction of all videos
    # transcoded" which would dilute toward 0 for libraries full of
    # playable mp4s.
    proxy_applicable = proxy_pending + proxy_running + proxy_done + proxy_failed

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

    # ---- geo estimation — "전체 대상" reads as "photos with no real
    #      (EXIF/user) GPS" so the matrix matches what a human looks
    #      at the gallery and counts. Photos missing taken_at can
    #      never be estimated (no time anchor) — they land in 'skipped'
    #      so the progress bar can actually reach 100% on the eligible
    #      subset. "done" = currently has an estimated location row.
    no_real_loc_subq = (
        select(PhotoLocation.photo_id).where(
            PhotoLocation.source.in_(("exif", "user"))
        )
    )
    geo_applicable = int(db.execute(
        select(func.count(Photo.id)).where(
            active,
            ~Photo.id.in_(no_real_loc_subq),
        )
    ).scalar() or 0)
    geo_skipped = int(db.execute(
        select(func.count(Photo.id)).where(
            active,
            Photo.taken_at.is_(None),
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
            # pending = applicable minus what's already estimated and
            # minus the can-never-be-estimated subset (no taken_at).
            # max(0,…) guards against the rare race where geo_done
            # races ahead of applicable between queries.
            pending=max(0, geo_applicable - geo_done - geo_skipped),
            running=_running("geo_estimate"),
            done=geo_done,
            failed=0,            # the estimator returns None silently on no anchor
            skipped=geo_skipped, # no taken_at — estimator can't even try
            extra={"no_taken_at": geo_skipped},
        ),
        StageRow(
            key="ml", label_key="indexing.stage_ml",
            applicable_total=total_images,
            pending=ml_b["pending"], running=_running("ml"),
            done=ml_b["done"], failed=ml_b["failed"],
            skipped=ml_b["skipped"], extra={},
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
            applicable_total=proxy_applicable,
            pending=proxy_pending,
            running=proxy_running,        # DB-side truth, not photo_work claimed
            done=proxy_done,
            failed=proxy_failed,
            # Videos with NULL proxy_status play natively → "skipped"
            # in the matrix sense (transcode doesn't apply to them).
            skipped=proxy_null,
            extra={"playable_native": proxy_null},
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


# ---------- per-stage retry trigger ---------------------------------


# Valid filter modes. Caller picks one:
#   failed  — photos where this stage's status column is 'failed'
#   pending — photos where it's 'pending' (or NULL on OCR-style stages)
#   all     — every photo this stage applies to (force re-run, dangerous)
_VALID_FILTERS = ("failed", "pending", "all")

# Valid stage keys — must match the matrix endpoint. Each maps to:
#   (status_column_name, photo_work_stage, applicable_predicate_kind)
# The dispatcher handler reads the column to pick eligible photos,
# resets it to 'pending', and enqueues the matching photo_work stage.
_STAGE_SPECS: dict[str, dict] = {
    "exif":         {"col": "exif_status",    "pw_stage": "index",            "scope": "all"},
    "thumb":        {"col": "thumb_status",   "pw_stage": "index",            "scope": "all"},
    "geo_estimate": {"col": None,             "pw_stage": "estimate_location","scope": "geo"},
    # "ml" filters on classify_status (the rollup) but the bulk-retry
    # handler resets all three underlying columns (objects/clip/faces)
    # so the worker actually re-runs each substage. "cols_reset" is
    # the dispatcher's hint; "col" is used by the API to filter on
    # the rollup for "실패만 / 미처리만" eligibility checks.
    "ml":           {"col": "classify_status", "pw_stage": "classify",        "scope": "image",
                     "cols_reset": ["objects_status", "clip_status", "faces_status"]},
    "ocr":          {"col": "ocr_status",     "pw_stage": "classify",         "scope": "image"},
    "transcode":    {"col": "proxy_status",   "pw_stage": "transcode",        "scope": "video"},
    # pair has no per-photo stage — the existing pair-companions admin
    # button stays the trigger. Listed here so the retry endpoint can
    # 400 cleanly when invoked for it.
    "pair":         {"col": None,             "pw_stage": None,               "scope": "pair"},
}


class RetryStageIn(BaseModel):
    stage: str
    filter: str = "failed"          # failed | pending | all
    # Only meaningful when stage == "ml" — subset of
    # ("objects", "clip", "faces"). If omitted, all three are retried
    # (same as the legacy ML card's default "분류 시작" with everything
    # checked).
    substages: Optional[list[str]] = None


class RetryStageOut(BaseModel):
    job_id: int
    stage: str
    filter: str
    eligible: int


@router.post("/retry-stage", response_model=RetryStageOut, status_code=202)
def retry_stage(
    body: RetryStageIn, db: Session = Depends(get_db),
) -> RetryStageOut:
    """Kick off a per-stage retry as a background bulk_retry_stage job.

    Returns immediately (202). The dispatcher's bulk_retry_stage
    handler walks the matching photos and fans them out into
    photo_work — same offload pattern as estimate-locations, so a
    big retry doesn't block the API request or starve workers via
    the API connection pool.
    """
    from fastapi import HTTPException, status as httpstatus

    if body.stage not in _STAGE_SPECS:
        raise HTTPException(httpstatus.HTTP_400_BAD_REQUEST,
                            f"unknown stage: {body.stage}")
    if body.filter not in _VALID_FILTERS:
        raise HTTPException(httpstatus.HTTP_400_BAD_REQUEST,
                            f"filter must be one of {_VALID_FILTERS}")
    spec = _STAGE_SPECS[body.stage]
    if spec["pw_stage"] is None:
        raise HTTPException(httpstatus.HTTP_400_BAD_REQUEST,
                            f"stage {body.stage!r} is not retry-able from the matrix; "
                            "use its dedicated admin button")

    # Normalise substages — only honoured for stage='ml'. Anything
    # else passed in is ignored so the API stays forgiving.
    substages: list[str] = []
    if body.stage == "ml" and body.substages:
        substages = [s for s in body.substages if s in ("objects", "clip", "faces")]

    # Cheap eligible COUNT(*) so the UI can show "예상 N건" right away
    # without waiting for the dispatcher to start.
    eligible = _count_eligible(db, body.stage, body.filter, substages=substages)

    # Coalesce: if a retry for this exact (stage, filter, substages) is
    # already queued/running, return its job id instead of stacking a
    # duplicate.
    from ..models import Job
    existing = db.execute(
        select(Job.id, Job.payload).where(
            Job.kind == "bulk_retry_stage",
            Job.status.in_(("queued", "running")),
        )
    ).all()
    sset = tuple(sorted(substages))
    for jid, payload_text in existing:
        try:
            pl = json.loads(payload_text or "{}")
        except (ValueError, TypeError):
            pl = {}
        if (pl.get("stage") == body.stage
                and pl.get("filter") == body.filter
                and tuple(sorted(pl.get("substages") or [])) == sset):
            return RetryStageOut(
                job_id=int(jid), stage=body.stage, filter=body.filter,
                eligible=eligible,
            )

    payload: dict = {"stage": body.stage, "filter": body.filter}
    if substages:
        payload["substages"] = substages
    job_id = int(jobs_mod.enqueue(
        db,
        kind="bulk_retry_stage",
        payload=payload,
        priority=70,
    ))
    db.commit()
    return RetryStageOut(
        job_id=job_id, stage=body.stage, filter=body.filter,
        eligible=eligible,
    )


def _count_eligible(
    db: Session, stage: str, filter_: str, *, substages: list[str] | None = None,
) -> int:
    """Mirror of the dispatcher's SELECT, used by the API to give the
    user an immediate "how many will this affect" number."""
    from sqlalchemy import or_ as _or

    spec = _STAGE_SPECS[stage]
    active = (Photo.status == "active")

    if stage == "geo_estimate":
        no_real_loc = (
            select(PhotoLocation.photo_id).where(
                PhotoLocation.source.in_(("exif", "user"))
            )
        )
        base = select(func.count(Photo.id)).where(
            active,
            Photo.taken_at.is_not(None),
            ~Photo.id.in_(no_real_loc),
        )
        if filter_ == "failed":
            return 0                     # estimator has no 'failed' state
        if filter_ == "pending":
            # Photos without any location row yet.
            no_loc = select(PhotoLocation.photo_id)
            return int(db.execute(base.where(~Photo.id.in_(no_loc))).scalar() or 0) \
                + int(db.execute(base.where(Photo.id.in_(no_loc))).scalar() or 0)
        return int(db.execute(base).scalar() or 0)

    q = select(func.count(Photo.id)).where(active)
    if spec["scope"] == "image":
        q = q.where(Photo.media_kind == "image")
    elif spec["scope"] == "video":
        q = q.where(Photo.media_kind == "video")

    # ML with a chosen subset of substages — filter on those columns
    # instead of the classify_status rollup. "전체" (filter='all')
    # still selects every image in scope; the rollup is only used
    # when no substages narrow it.
    if stage == "ml" and substages:
        col_map = {
            "objects": Photo.objects_status,
            "clip":    Photo.clip_status,
            "faces":   Photo.faces_status,
        }
        chosen = [col_map[s] for s in substages if s in col_map]
        if chosen:
            if filter_ == "failed":
                q = q.where(_or(*[c == "failed" for c in chosen]))
            elif filter_ == "pending":
                q = q.where(_or(*[c == "pending" for c in chosen]))
            # "all" → no extra filter
            return int(db.execute(q).scalar() or 0)

    col = getattr(Photo, spec["col"])
    if filter_ == "failed":
        q = q.where(col == "failed")
    elif filter_ == "pending":
        if stage == "ocr":               # OCR uses NULL as pending
            q = q.where((col == "pending") | (col.is_(None)))
        else:
            q = q.where(col == "pending")
    # "all" → no extra filter
    return int(db.execute(q).scalar() or 0)

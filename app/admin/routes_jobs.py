"""Minimal jobs visibility — see queue depth and recent failures."""

from __future__ import annotations

import json
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import delete as _delete
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from ..api.deps import get_db
from ..auth import require_admin
from ..models import Job, Photo, User

router = APIRouter(prefix="/admin/jobs", tags=["admin", "jobs"])

# Statuses safe to wipe by default. `running` requires opt-in because a
# worker may still be mid-job — re-claiming it would clash on the
# claim_token. `done` is excluded so audit trail is not silently lost.
_PURGEABLE_DEFAULT = {"queued", "failed"}
_PURGEABLE_ALL = {"queued", "failed", "running", "done"}


class JobStats(BaseModel):
    queued: int
    running: int
    failed: int
    done: int


class JobOut(BaseModel):
    id: int
    kind: str
    status: str
    attempts: int
    last_error: str | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None

    class Config:
        from_attributes = True


@router.get("/stats", response_model=JobStats)
def stats(db: Session = Depends(get_db)) -> JobStats:
    rows = dict(
        db.execute(select(Job.status, func.count(Job.id)).group_by(Job.status)).all()
    )
    return JobStats(
        queued=rows.get("queued", 0),
        running=rows.get("running", 0),
        failed=rows.get("failed", 0),
        done=rows.get("done", 0),
    )


@router.get("/recent", response_model=list[JobOut])
def recent(
    status_filter: str | None = None,
    limit: int = 50,
    db: Session = Depends(get_db),
) -> list[JobOut]:
    q = select(Job).order_by(Job.id.desc()).limit(min(limit, 500))
    if status_filter:
        q = q.where(Job.status == status_filter)
    return [JobOut.model_validate(r) for r in db.execute(q).scalars().all()]


class PhotoIndexStats(BaseModel):
    """Indexing progress per stage. All counts scope to active photos only."""

    total_active: int
    exif_pending: int
    exif_ok: int
    exif_partial: int
    exif_failed: int
    exif_skipped: int
    thumb_pending: int
    thumb_ok: int
    thumb_failed: int
    thumb_skipped: int


@router.get("/photo-stats", response_model=PhotoIndexStats)
def photo_stats(db: Session = Depends(get_db)) -> PhotoIndexStats:
    """Per-stage counts for the indexing dashboard.

    Mirrors the EXIF/thumb status fields on Photo so the admin UI can
    show a progress bar (`ok / total`) and how many retries are pending.
    """
    from ..models import Photo

    exif = dict(
        db.execute(
            select(Photo.exif_status, func.count(Photo.id))
            .where(Photo.status == "active")
            .group_by(Photo.exif_status)
        ).all()
    )
    thumb = dict(
        db.execute(
            select(Photo.thumb_status, func.count(Photo.id))
            .where(Photo.status == "active")
            .group_by(Photo.thumb_status)
        ).all()
    )
    total = sum(exif.values())
    return PhotoIndexStats(
        total_active=total,
        exif_pending=exif.get("pending", 0),
        exif_ok=exif.get("ok", 0),
        exif_partial=exif.get("partial", 0),
        exif_failed=exif.get("failed", 0),
        exif_skipped=exif.get("skipped", 0),
        thumb_pending=thumb.get("pending", 0),
        thumb_ok=thumb.get("ok", 0),
        thumb_failed=thumb.get("failed", 0),
        thumb_skipped=thumb.get("skipped", 0),
    )


class RetryRequest(BaseModel):
    # Which stage(s) to retry. 'exif' or 'thumb' (or both).
    stages: list[str] = ["exif", "thumb"]
    # Photo filter — re-enqueue every photo whose status is in the list.
    # e.g. ['failed', 'partial'] to retry both failure modes at once.
    exif_status: list[str] = []
    thumb_status: list[str] = []
    root_id: int | None = None
    limit: int = 1000


class RetryResponse(BaseModel):
    matched: int
    enqueued: int


class PurgeRequest(BaseModel):
    """Body for /api/admin/jobs/purge.

    `statuses` defaults to {queued, failed}. To also wipe `running` jobs
    (e.g. when a stuck job is blocking the queue after a misconfigured
    earlier run), set `include_running=True`. `kind` further narrows the
    delete to a single job kind (e.g. 'discover_root', 'index_file',
    'classify_ml').
    """

    statuses: list[str] | None = None
    include_running: bool = False
    kind: str | None = None


class PurgeResponse(BaseModel):
    deleted: int
    statuses: list[str]
    kind: str | None


@router.post("/purge", response_model=PurgeResponse)
def purge_jobs(body: PurgeRequest | None = None, db: Session = Depends(get_db)) -> PurgeResponse:
    """Delete jobs matching the given statuses (default queued+failed).

    Use case: an earlier scan was launched with a wrong root path or
    permissions, the original jobs failed but new attempts can't start
    because the queue is still full of dead entries. Purge clears them
    so a fresh scan can run.
    """
    req = body or PurgeRequest()
    statuses = set(req.statuses or _PURGEABLE_DEFAULT)
    if req.include_running:
        statuses.add("running")
    unknown = statuses - _PURGEABLE_ALL
    if unknown:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"알 수 없는 상태: {sorted(unknown)} (허용: {sorted(_PURGEABLE_ALL)})",
        )
    if not statuses:
        return PurgeResponse(deleted=0, statuses=[], kind=req.kind)

    stmt = _delete(Job).where(Job.status.in_(statuses))
    if req.kind:
        stmt = stmt.where(Job.kind == req.kind)
    result = db.execute(stmt)
    db.commit()
    return PurgeResponse(
        deleted=int(result.rowcount or 0),
        statuses=sorted(statuses),
        kind=req.kind,
    )


# Browser-undecodable *containers* — no browser plays these regardless of
# the inner codec, so they're safe to transcode proactively. HEVC inside
# mp4/mov is browser-dependent (Safari plays it) so it stays lazy.
_UNPLAYABLE_VIDEO_EXTS = (".avi", ".mkv", ".3gp")


class TranscodeBackfillResponse(BaseModel):
    candidates: int   # unplayable-container videos still needing a proxy
    enqueued: int     # builds queued by this call
    skipped: int      # candidates already queued/running (left alone)


@router.post("/transcode-backfill", response_model=TranscodeBackfillResponse)
def transcode_backfill(db: Session = Depends(get_db)) -> TranscodeBackfillResponse:
    """Queue H.264 proxy builds for every browser-undecodable video
    (.avi/.mkv/.3gp) that has no usable proxy yet — i.e. proxy_status is
    NULL (never tried) or 'failed' (retry). Idempotent: a video already
    queued/running is left alone. HEVC mp4/mov stays lazy (browser-built
    on first view)."""
    from ..worker import jobs as jobs_mod

    rows = db.execute(
        select(Photo.id, Photo.rel_path).where(
            Photo.media_kind == "video",
            Photo.status == "active",
            or_(Photo.proxy_status.is_(None), Photo.proxy_status == "failed"),
        )
    ).all()

    # photo_ids that already have a transcode job in flight (avoid dups).
    inflight: set[int] = set()
    for (payload,) in db.execute(
        select(Job.payload).where(
            Job.kind == "transcode_proxy",
            Job.status.in_(("queued", "running")),
        )
    ).all():
        try:
            inflight.add(int(json.loads(payload)["photo_id"]))
        except (ValueError, KeyError, TypeError):
            pass

    candidates = enqueued = skipped = 0
    for pid, rel in rows:
        if not (rel and rel.lower().endswith(_UNPLAYABLE_VIDEO_EXTS)):
            continue
        candidates += 1
        if pid in inflight:
            skipped += 1
            continue
        jobs_mod.enqueue(db, kind="transcode_proxy",
                         payload={"photo_id": pid}, priority=3)
        p = db.get(Photo, pid)
        if p is not None:
            p.proxy_status = "pending"
            p.proxy_error = None
        enqueued += 1
    db.commit()
    return TranscodeBackfillResponse(
        candidates=candidates, enqueued=enqueued, skipped=skipped)


class PairCompanionsResponse(BaseModel):
    scanned: int
    paired: int


@router.post("/pair-companions", response_model=PairCompanionsResponse)
def pair_companions(db: Session = Depends(get_db)) -> PairCompanionsResponse:
    """One-shot backfill: walk every still-unpaired photo and try to link
    HEIC↔MOV (or other image↔video) pairs that share root + parent dir +
    filename stem and were created within ~24 h of each other.

    Idempotent. Safe to re-run after a re-scan adds more files.
    """
    from ..models import Photo
    from ..scanner.discover import _LIVE_STILL_EXTS, _LIVE_VIDEO_EXTS

    def _live_eligible(p) -> bool:
        ext = (p.ext or "").lower().lstrip(".")
        if p.media_kind == "image":
            return ext in _LIVE_STILL_EXTS
        return ext in _LIVE_VIDEO_EXTS

    def _live_pair(a, b) -> bool:
        # One must be image-kind with a still ext, the other video-
        # kind with the video ext. Order-agnostic.
        if a.media_kind == "image" and b.media_kind == "video":
            still, vid = a, b
        elif a.media_kind == "video" and b.media_kind == "image":
            still, vid = b, a
        else:
            return False
        still_ext = (still.ext or "").lower().lstrip(".")
        vid_ext = (vid.ext or "").lower().lstrip(".")
        return still_ext in _LIVE_STILL_EXTS and vid_ext in _LIVE_VIDEO_EXTS

    scanned = 0
    paired = 0
    rows = db.execute(
        select(Photo).where(Photo.companion_id.is_(None))
    ).scalars().all()
    # In-memory index keyed by (root_id, parent_dir, stem) so we don't
    # query the DB once per photo (would be ~10⁵ selects on a real
    # library). Each entry maps the key to the first photo seen there.
    index: dict[tuple[int, str, str], Photo] = {}
    for p in rows:
        scanned += 1
        name = p.filename or ""
        if "." not in name:
            continue
        stem = name.rsplit(".", 1)[0]
        if not stem:
            continue
        # Cheap ext gate before bothering the index — only iPhone Live
        # Photo extensions ever pair.
        if not _live_eligible(p):
            continue
        parent = p.rel_path.rsplit("/", 1)[0] if "/" in p.rel_path else ""
        key = (p.root_id, parent, stem)
        partner = index.get(key)
        if partner is None:
            index[key] = p
            continue
        # Two photos with the same stem in the same dir — pair only if
        # they form a valid Live Photo (HEIC|JPG ↔ MOV) and were
        # written within ~24h of each other.
        if not _live_pair(partner, p):
            index[key] = p
            continue
        if partner.mtime and p.mtime:
            if abs((partner.mtime - p.mtime).total_seconds()) > 86400:
                index[key] = p
                continue
        p.companion_id = partner.id
        partner.companion_id = p.id
        paired += 1
        # Now that both are paired, evict from the index so a fourth
        # file at the same key doesn't accidentally re-pair.
        index.pop(key, None)
    db.commit()
    return PairCompanionsResponse(scanned=scanned, paired=paired)


class FailedPhotoOut(BaseModel):
    id: int
    root_label: str
    rel_path: str
    filename: str
    media_kind: str
    exif_status: str
    thumb_status: str
    classify_status: str
    exif_error: str | None = None
    thumb_error: str | None = None
    error: str | None = None      # whichever stage's error fits the filter


class FailedPhotosPage(BaseModel):
    total: int
    page: int
    page_size: int
    items: list[FailedPhotoOut]


@router.get("/failed-photos", response_model=FailedPhotosPage)
def failed_photos(
    stage: str = "exif",
    page: int = 1,
    page_size: int = 100,
    db: Session = Depends(get_db),
) -> FailedPhotosPage:
    """List photos whose pipeline stage is `failed`, with the worker's
    error message. Admin uses this to spot junk files (wrong format,
    permission issues, corrupt headers) and bulk-delete them.

    stage must be one of `exif`, `thumb`, `classify`.
    """
    from ..models import Photo, Root

    page = max(1, page)
    page_size = max(1, min(page_size, 500))
    stage = stage.lower()
    if stage not in ("exif", "thumb", "classify"):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "stage는 exif / thumb / classify 중 하나여야 합니다",
        )
    status_col = {
        "exif": Photo.exif_status,
        "thumb": Photo.thumb_status,
        "classify": Photo.classify_status,
    }[stage]

    base = (
        select(Photo, Root.label)
        .join(Root, Root.id == Photo.root_id)
        .where(Photo.status == "active", status_col == "failed")
    )
    total = db.execute(
        select(func.count()).select_from(base.subquery())
    ).scalar_one()
    rows = db.execute(
        base.order_by(Photo.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    ).all()
    items: list[FailedPhotoOut] = []
    for p, root_label in rows:
        # classify_status has no dedicated error column on Photo; reuse
        # exif_error / thumb_error for those stages and fall back to None.
        primary_err = (
            p.exif_error if stage == "exif"
            else p.thumb_error if stage == "thumb"
            else None
        )
        items.append(FailedPhotoOut(
            id=p.id,
            root_label=root_label,
            rel_path=p.rel_path,
            filename=p.filename,
            media_kind=p.media_kind,
            exif_status=p.exif_status,
            thumb_status=p.thumb_status,
            classify_status=p.classify_status,
            exif_error=p.exif_error,
            thumb_error=p.thumb_error,
            error=primary_err,
        ))
    return FailedPhotosPage(
        total=int(total or 0), page=page, page_size=page_size, items=items,
    )


class TrashAllFailedRequest(BaseModel):
    """Request shape for trashing every failed photo of a given stage.

    Same `stage` vocabulary as /failed-photos (exif / thumb / classify).
    No pagination — the endpoint touches every photo whose pipeline
    bucket is `failed`. Chunks internally so a single transaction
    doesn't grow unbounded.
    """

    stage: str


class TrashAllFailedResponse(BaseModel):
    candidates: int                 # rows matching the filter pre-write
    trashed: int                    # rows actually moved to data/trash/
    skipped_readonly: list[int]     # photo_ids on readonly roots
    failed: list[dict]              # [{id, reason}]


@router.post("/failed-photos/trash-all", response_model=TrashAllFailedResponse)
def trash_all_failed_photos(
    body: TrashAllFailedRequest,
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
) -> TrashAllFailedResponse:
    """Bulk-trash EVERY photo whose `<stage>_status='failed'` — the
    "정리" button for the indexing failure dashboard.

    Used when a sweep of corrupt / unreadable / wrong-format files needs
    one-click cleanup instead of the page-by-page selection loop.

    Chunked at 500 per inner trash_photos_core() call so a 50k cleanup
    doesn't hold a single transaction open for minutes. Skipped (read-
    only root) and failed (permission / file-missing) outcomes are
    aggregated across chunks so the client sees one summary.

    Admin-only — both via the router-level require_admin guard AND the
    require_admin dependency below (so the User row reaches
    trash_photos_core for audit-log attribution).
    """
    from ..api.routes_photos import trash_photos_core
    from ..models import Photo

    stage = (body.stage or "").lower()
    if stage not in ("exif", "thumb", "classify"):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "stage는 exif / thumb / classify 중 하나여야 합니다",
        )
    status_col = {
        "exif": Photo.exif_status,
        "thumb": Photo.thumb_status,
        "classify": Photo.classify_status,
    }[stage]

    photo_ids = [
        r[0]
        for r in db.execute(
            select(Photo.id).where(
                Photo.status == "active",
                status_col == "failed",
            )
        ).all()
    ]
    if not photo_ids:
        return TrashAllFailedResponse(
            candidates=0, trashed=0, skipped_readonly=[], failed=[],
        )

    CHUNK = 500
    trashed_total = 0
    all_failed: list[dict] = []
    all_skipped: list[int] = []
    for i in range(0, len(photo_ids), CHUNK):
        batch = photo_ids[i:i + CHUNK]
        result = trash_photos_core(db, batch, user)
        trashed_total += int(result.get("deleted", 0))
        all_failed.extend(result.get("failed", []))
        all_skipped.extend(result.get("skipped_readonly", []))

    return TrashAllFailedResponse(
        candidates=len(photo_ids),
        trashed=trashed_total,
        skipped_readonly=all_skipped,
        failed=all_failed,
    )


@router.post("/retry-photos", response_model=RetryResponse)
def retry_photos(body: RetryRequest, db: Session = Depends(get_db)) -> RetryResponse:
    """Reset selected photos' stage status to 'pending' and enqueue
    index_file jobs for them. Useful after installing exiftool/ffmpeg/
    pillow-heif to recover previously failed extractions.
    """
    from sqlalchemy import update

    from ..models import Photo
    from ..worker.jobs import enqueue_many

    # Skip trashed / missing photos — re-enqueueing index_file for them
    # used to silently overwrite status='trashed' → 'missing' (their
    # file lives in data/trash/, not at root.abs_path/rel_path), which
    # detached them from the trash UI and made restore impossible. The
    # index_file handler now bails out for status='trashed' even if a
    # stale job hits it, but the cleaner fix is to never enqueue.
    q = select(Photo.id).where(Photo.status == "active")
    if body.root_id is not None:
        q = q.where(Photo.root_id == body.root_id)
    if body.exif_status:
        q = q.where(Photo.exif_status.in_(body.exif_status))
    if body.thumb_status:
        q = q.where(Photo.thumb_status.in_(body.thumb_status))
    q = q.limit(min(body.limit, 100_000))

    photo_ids = [r[0] for r in db.execute(q).all()]
    if not photo_ids:
        return RetryResponse(matched=0, enqueued=0)

    reset_values: dict = {}
    if "exif" in body.stages:
        reset_values["exif_status"] = "pending"
        reset_values["exif_error"] = None
    if "thumb" in body.stages:
        reset_values["thumb_status"] = "pending"
        reset_values["thumb_error"] = None

    # Chunk the work so the SQLite writer lock is released between batches —
    # the indexing/ML workers are continuously writing, and a single 1000-row
    # UPDATE + 1000-row INSERT in one transaction can starve them out and
    # itself hit `database is locked` after the busy_timeout.
    CHUNK = 200
    enqueued = 0
    for i in range(0, len(photo_ids), CHUNK):
        chunk = photo_ids[i:i + CHUNK]
        if reset_values:
            db.execute(update(Photo).where(Photo.id.in_(chunk)).values(**reset_values))
        enqueued += enqueue_many(
            db,
            kind="index_file",
            payloads=[{"photo_id": pid} for pid in chunk],
            priority=5,
        )
        db.commit()
    return RetryResponse(matched=len(photo_ids), enqueued=enqueued)

"""Minimal jobs visibility — see queue depth and recent failures."""

from __future__ import annotations

import json
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, status
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


class KindStat(BaseModel):
    """Queue counts for one job kind, for the per-kind donuts in the admin
    panel. `worker` is which service claims it (ml / index / other)."""
    kind: str
    worker: str
    queued: int = 0
    running: int = 0
    failed: int = 0
    done: int = 0


class JobStats(BaseModel):
    # Totals across every kind (the outer ring of the job-queue donut).
    queued: int
    running: int
    failed: int
    done: int
    # Split by which worker owns the kind, for the nested inner rings.
    index_queued: int = 0
    index_running: int = 0
    index_failed: int = 0
    index_done: int = 0
    ml_queued: int = 0
    ml_running: int = 0
    ml_failed: int = 0
    ml_done: int = 0
    # Per-kind breakdown (every kind seen in the queue), for the kind donuts.
    by_kind: list[KindStat] = []


# Which worker claims which kinds (mirror of the two dispatchers). The
# legacy index_*/ml_* aggregates above use the narrow sets (kept stable);
# the per-kind view below uses the COMPLETE sets so ocr_text / reindex_fts
# / recluster_faces aren't dropped.
_INDEX_JOB_KINDS = {"index_file", "discover_root", "dedup_cleanup", "transcode_proxy"}
_ML_JOB_KINDS = {"classify_objects", "classify_embedding", "detect_faces"}
_INDEX_KINDS_ALL = _INDEX_JOB_KINDS | {"reindex_fts"}
_ML_KINDS_ALL = _ML_JOB_KINDS | {"ocr_text", "recluster_faces"}


def _worker_of(kind: str) -> str:
    if kind in _ML_KINDS_ALL:
        return "ml"
    if kind in _INDEX_KINDS_ALL:
        return "index"
    return "other"


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
    rows = db.execute(
        select(Job.kind, Job.status, func.count(Job.id)).group_by(Job.kind, Job.status)
    ).all()
    tot: dict[str, int] = {}
    idx: dict[str, int] = {}
    mlc: dict[str, int] = {}
    per: dict[str, dict[str, int]] = {}
    for kind, st, n in rows:
        tot[st] = tot.get(st, 0) + n
        if kind in _INDEX_JOB_KINDS:
            idx[st] = idx.get(st, 0) + n
        elif kind in _ML_JOB_KINDS:
            mlc[st] = mlc.get(st, 0) + n
        per.setdefault(kind, {})[st] = per.setdefault(kind, {}).get(st, 0) + n

    # Per-kind list: ML kinds first, then index, then other; within a group
    # the busiest (most in-flight) first so backlogs surface at the top.
    _grp = {"ml": 0, "index": 1, "other": 2}
    by_kind = [
        KindStat(
            kind=k, worker=_worker_of(k),
            queued=v.get("queued", 0), running=v.get("running", 0),
            failed=v.get("failed", 0), done=v.get("done", 0),
        )
        for k, v in per.items()
    ]
    by_kind.sort(key=lambda s: (_grp.get(s.worker, 9), -(s.queued + s.running + s.failed)))
    return JobStats(
        by_kind=by_kind,
        queued=tot.get("queued", 0), running=tot.get("running", 0),
        failed=tot.get("failed", 0), done=tot.get("done", 0),
        index_queued=idx.get("queued", 0), index_running=idx.get("running", 0),
        index_failed=idx.get("failed", 0), index_done=idx.get("done", 0),
        ml_queued=mlc.get("queued", 0), ml_running=mlc.get("running", 0),
        ml_failed=mlc.get("failed", 0), ml_done=mlc.get("done", 0),
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
    thumb_partial: int
    thumb_failed: int
    thumb_skipped: int
    # Classification (YOLO/CLIP/face) — ok | failed | skipped | pending.
    classify_pending: int = 0
    classify_ok: int = 0
    classify_failed: int = 0
    classify_skipped: int = 0
    # Video proxy (transcode) — scoped to active *videos*.
    video_total: int = 0
    proxy_done: int = 0
    proxy_failed: int = 0
    proxy_pending: int = 0   # pending + running
    proxy_none: int = 0      # NULL — playable as-is / never requested


@router.get("/photo-stats", response_model=PhotoIndexStats)
def photo_stats(db: Session = Depends(get_db)) -> PhotoIndexStats:
    """Per-stage counts for the indexing dashboard.

    Mirrors the EXIF/thumb status fields on Photo so the admin UI can
    show a progress bar (`ok / total`) and how many retries are pending.
    """
    from sqlalchemy import case

    from ..models import Photo

    def _n(cond):
        # COUNT of active photos matching cond, as one column of a single
        # table scan (vs the old 4 separate GROUP BY scans over 362k rows).
        return func.sum(case((cond, 1), else_=0))

    is_video = Photo.media_kind == "video"
    row = db.execute(
        select(
            func.count().label("total_active"),
            _n(Photo.exif_status == "pending").label("exif_pending"),
            _n(Photo.exif_status == "ok").label("exif_ok"),
            _n(Photo.exif_status == "partial").label("exif_partial"),
            _n(Photo.exif_status == "failed").label("exif_failed"),
            _n(Photo.exif_status == "skipped").label("exif_skipped"),
            _n(Photo.thumb_status == "pending").label("thumb_pending"),
            _n(Photo.thumb_status == "ok").label("thumb_ok"),
            _n(Photo.thumb_status == "partial").label("thumb_partial"),
            _n(Photo.thumb_status == "failed").label("thumb_failed"),
            _n(Photo.thumb_status == "skipped").label("thumb_skipped"),
            _n(Photo.classify_status == "pending").label("classify_pending"),
            _n(Photo.classify_status == "ok").label("classify_ok"),
            _n(Photo.classify_status == "failed").label("classify_failed"),
            _n(Photo.classify_status == "skipped").label("classify_skipped"),
            _n(is_video).label("video_total"),
            _n(is_video & (Photo.proxy_status == "done")).label("proxy_done"),
            _n(is_video & (Photo.proxy_status == "failed")).label("proxy_failed"),
            _n(is_video & Photo.proxy_status.in_(("pending", "running"))).label("proxy_pending"),
            _n(is_video & Photo.proxy_status.is_(None)).label("proxy_none"),
        ).where(Photo.status == "active")
    ).mappings().one()

    def _g(k):
        return int(row[k] or 0)

    return PhotoIndexStats(
        total_active=_g("total_active"),
        exif_pending=_g("exif_pending"), exif_ok=_g("exif_ok"),
        exif_partial=_g("exif_partial"), exif_failed=_g("exif_failed"),
        exif_skipped=_g("exif_skipped"),
        thumb_pending=_g("thumb_pending"), thumb_ok=_g("thumb_ok"),
        thumb_partial=_g("thumb_partial"), thumb_failed=_g("thumb_failed"),
        thumb_skipped=_g("thumb_skipped"),
        classify_pending=_g("classify_pending"), classify_ok=_g("classify_ok"),
        classify_failed=_g("classify_failed"), classify_skipped=_g("classify_skipped"),
        video_total=_g("video_total"),
        proxy_done=_g("proxy_done"), proxy_failed=_g("proxy_failed"),
        proxy_pending=_g("proxy_pending"), proxy_none=_g("proxy_none"),
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
    proxy_status: str | None = None
    exif_error: str | None = None
    thumb_error: str | None = None
    proxy_error: str | None = None
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


# Map a (stage, status) filter onto the right Photo column + value.
# Note the per-stage vocabularies: exif/thumb use 'ok'/'partial'/'failed';
# classify has no 'partial'; proxy uses 'done' (not 'ok') and has no 'partial'.
_STAGE_STATUS_VALUE = {
    "exif":     {"done": "ok",   "partial": "partial", "failed": "failed"},
    "thumb":    {"done": "ok",   "partial": "partial", "failed": "failed"},
    "classify": {"done": "ok",                          "failed": "failed"},
    "proxy":    {"done": "done",                         "failed": "failed"},
}


@router.get("/photos-by-status", response_model=FailedPhotosPage)
def photos_by_status(
    stage: str = "thumb",
    status_: str = Query("failed", alias="status"),
    page: int = 1,
    page_size: int = 100,
    db: Session = Depends(get_db),
) -> FailedPhotosPage:
    """List active photos at a given pipeline `stage` whose status matches
    `status_` (done / partial / failed). Powers the admin status-list page
    and the click-through from the indexing donuts.

    stage ∈ exif | thumb | classify | proxy. Combos with no such status
    (classify/proxy have no 'partial') return an empty page.
    """
    from ..models import Photo, Root

    page = max(1, page)
    page_size = max(1, min(page_size, 500))
    stage = stage.lower()
    status_ = (status_ or "").lower()
    if stage not in _STAGE_STATUS_VALUE:
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            "stage는 exif / thumb / classify / proxy 중 하나여야 합니다")
    value = _STAGE_STATUS_VALUE[stage].get(status_)
    if value is None:
        # e.g. classify/proxy + 'partial' — valid request, just no rows.
        return FailedPhotosPage(total=0, page=page, page_size=page_size, items=[])

    col = {
        "exif": Photo.exif_status, "thumb": Photo.thumb_status,
        "classify": Photo.classify_status, "proxy": Photo.proxy_status,
    }[stage]
    base = (
        select(Photo, Root.label)
        .join(Root, Root.id == Photo.root_id)
        .where(Photo.status == "active", col == value)
    )
    if stage == "proxy":
        base = base.where(Photo.media_kind == "video")
    total = db.execute(select(func.count()).select_from(base.subquery())).scalar_one()
    rows = db.execute(
        base.order_by(Photo.id.desc())
        .offset((page - 1) * page_size).limit(page_size)
    ).all()
    items: list[FailedPhotoOut] = []
    for p, root_label in rows:
        primary = (
            p.exif_error if stage == "exif"
            else p.thumb_error if stage == "thumb"
            else p.proxy_error if stage == "proxy"
            else None
        )
        items.append(FailedPhotoOut(
            id=p.id, root_label=root_label, rel_path=p.rel_path,
            filename=p.filename, media_kind=p.media_kind,
            exif_status=p.exif_status, thumb_status=p.thumb_status,
            classify_status=p.classify_status, proxy_status=p.proxy_status,
            exif_error=p.exif_error, thumb_error=p.thumb_error,
            proxy_error=p.proxy_error, error=primary,
        ))
    return FailedPhotosPage(total=int(total or 0), page=page,
                            page_size=page_size, items=items)


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

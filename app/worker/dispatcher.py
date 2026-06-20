"""Worker dispatcher.

Spawns N threads that each pull one job at a time from the SQLite-backed
queue. Each handler is registered by kind. New kinds added in later MVPs
(e.g. 'discover_root', 'rethumb', 'verify') just register here.

SQLite handles read concurrency well; writes are serialised by the file
lock and our short critical sections. WAL keeps reads non-blocking.
"""

from __future__ import annotations

import logging
import threading
from typing import Callable

from ..config import get_settings
from ..db import SessionLocal
from ..models import Root
from ..scanner.discover import discover_root
from . import dedup_cleanup as dedup_cleanup_handler
from . import exiftool_pool
from . import jobs as jobs_mod
from . import photo_work as photo_work_mod

log = logging.getLogger(__name__)


Handler = Callable[["Session", dict], None]


HANDLERS: dict[str, Handler] = {}


def _handle_discover_root(db, payload: dict) -> None:
    root_id = int(payload["root_id"])
    root = db.get(Root, root_id)
    if root is None:
        log.warning("discover_root: root %d not found", root_id)
        return
    limit = payload.get("limit")
    discover_root(db, root, limit=limit)


def _handle_reindex_fts(db, payload: dict) -> None:
    from .. import fts as _fts
    n = _fts.reindex_all(db)
    log.info("reindex_fts: rebuilt %d photo FTS rows", n)


# ---------- legacy drain shims --------------------------------------
#
# Phase 3 stopped enqueueing these kinds (everything routes through
# photo_work now) but old rows may still be sitting in `jobs` from
# before the upgrade. Each shim forwards to the equivalent photo_work
# stage and marks the legacy job done. After the legacy queue drains
# (admin → 작업 큐 → kind별 카운트 0), the shims + their HANDLERS
# entries can be deleted in a future release.


def _drain_index_file(db, payload: dict) -> None:
    photo_id = int(payload["photo_id"])
    photo_work_mod.enqueue_stage(db, photo_id=photo_id, stage="index", priority=5)
    db.commit()


def _drain_transcode_proxy(db, payload: dict) -> None:
    photo_id = int(payload["photo_id"])
    photo_work_mod.enqueue_stage(db, photo_id=photo_id, stage="transcode", priority=5)
    db.commit()


def _drain_estimate_photo_location(db, payload: dict) -> None:
    photo_id = int(payload["photo_id"])
    params = {}
    if "threshold_seconds" in payload:
        params["threshold_seconds"] = int(payload["threshold_seconds"])
    photo_work_mod.enqueue_stage(
        db, photo_id=photo_id, stage="estimate_location", priority=10,
        params=params or None,
    )
    db.commit()


def _handle_bulk_retry_stage(db, payload: dict) -> None:
    """Per-stage retry fan-out — admin matrix's "재작업 / 미처리 / 전체"
    action sends one of these. Walks the eligible photo set for the
    chosen stage + filter, resets the relevant status column to
    'pending' (so the legacy *_status views agree), and enqueues the
    matching photo_work stage. Same offload pattern as the geo
    drain — keeps big bulk operations out of the API request path.
    """
    from ..models import Photo, PhotoLocation
    from sqlalchemy import select as _select, update as _update

    stage = str(payload.get("stage") or "")
    filt = str(payload.get("filter") or "failed")

    # Stage → (status column name, photo_work stage, scope predicate).
    # Mirrors app/admin/routes_indexing._STAGE_SPECS — keep these two
    # tables in sync; tiny enough that a shared module isn't worth it.
    spec_table: dict[str, dict] = {
        "exif":         {"col": "exif_status",    "pw_stage": "index",            "scope": "all"},
        "thumb":        {"col": "thumb_status",   "pw_stage": "index",            "scope": "all"},
        "geo_estimate": {"col": None,             "pw_stage": "estimate_location","scope": "geo"},
        "ml_objects":   {"col": "objects_status", "pw_stage": "classify",         "scope": "image"},
        "ml_clip":      {"col": "clip_status",    "pw_stage": "classify",         "scope": "image"},
        "ml_faces":     {"col": "faces_status",   "pw_stage": "classify",         "scope": "image"},
        "ocr":          {"col": "ocr_status",     "pw_stage": "classify",         "scope": "image"},
        "transcode":    {"col": "proxy_status",   "pw_stage": "transcode",        "scope": "video"},
    }
    spec = spec_table.get(stage)
    if spec is None:
        log.warning("bulk_retry_stage: unknown stage %r — skipping", stage)
        return

    active = (Photo.status == "active")
    base_q = _select(Photo.id).where(active)
    if spec["scope"] == "image":
        base_q = base_q.where(Photo.media_kind == "image")
    elif spec["scope"] == "video":
        base_q = base_q.where(Photo.media_kind == "video")
    elif spec["scope"] == "geo":
        no_real_loc = (
            _select(PhotoLocation.photo_id).where(
                PhotoLocation.source.in_(("exif", "user"))
            )
        )
        base_q = base_q.where(
            Photo.taken_at.is_not(None),
            ~Photo.id.in_(no_real_loc),
        )

    # Filter by status column. geo_estimate's "failed" is empty (the
    # estimator returns None silently on no anchor — there's no
    # 'failed' state to retry), so route it through "all" instead.
    if spec["col"] is not None:
        col = getattr(Photo, spec["col"])
        if filt == "failed":
            base_q = base_q.where(col == "failed")
        elif filt == "pending":
            if stage == "ocr":
                base_q = base_q.where((col == "pending") | (col.is_(None)))
            else:
                base_q = base_q.where(col == "pending")
        # "all" → no extra filter

    rows = db.execute(base_q).all()
    photo_ids = [int(pid) for (pid,) in rows]

    # Reset the status column to 'pending' in chunks so we don't hold
    # one fat write transaction. Skip this for geo_estimate (no
    # column) and for the OCR "all" case where NULL-meaning-pending
    # would lose context.
    if spec["col"] is not None and photo_ids:
        col_name = spec["col"]
        CHUNK = 500
        for i in range(0, len(photo_ids), CHUNK):
            chunk = photo_ids[i : i + CHUNK]
            db.execute(
                _update(Photo)
                .where(Photo.id.in_(chunk))
                .values({col_name: "pending"})
            )
            db.commit()

    # Fan out into photo_work — one row per photo, dedup-by-PK.
    pw_stage = spec["pw_stage"]
    enqueued = 0
    for pid in photo_ids:
        photo_work_mod.enqueue_stage(
            db, photo_id=pid, stage=pw_stage, priority=10,
        )
        enqueued += 1
        if enqueued % 500 == 0:
            db.commit()
    db.commit()
    log.info(
        "bulk_retry_stage: stage=%s filter=%s pendinged=%d photo_work_enqueued=%d",
        stage, filt, len(photo_ids), enqueued,
    )


def _drain_estimate_locations(db, payload: dict) -> None:
    """Root-level fan-out shim. Mirrors routes_roots.trigger_estimate_locations:
    select eligible photos and enqueue the photo_work stage for each."""
    from sqlalchemy import select as _select
    from ..models import Photo, PhotoLocation

    root_id = int(payload["root_id"])
    params = {}
    if "threshold_seconds" in payload:
        params["threshold_seconds"] = int(payload["threshold_seconds"])
    rows = db.execute(
        _select(Photo.id)
        .outerjoin(PhotoLocation, PhotoLocation.photo_id == Photo.id)
        .where(
            Photo.root_id == root_id,
            Photo.taken_at.is_not(None),
            Photo.exif_status.in_(("ok", "partial")),
            (PhotoLocation.photo_id.is_(None))
            | (PhotoLocation.source == "estimated"),
        )
    ).all()
    enqueued = 0
    for (pid,) in rows:
        photo_work_mod.enqueue_stage(
            db, photo_id=int(pid), stage="estimate_location", priority=10,
            params=params or None,
        )
        enqueued += 1
        if enqueued % 500 == 0:
            db.commit()
    db.commit()
    log.info(
        "legacy estimate_locations drain: root=%d → photo_work x %d",
        root_id, enqueued,
    )


HANDLERS["discover_root"] = _handle_discover_root
HANDLERS["dedup_cleanup"] = dedup_cleanup_handler.run
HANDLERS["reindex_fts"] = _handle_reindex_fts
HANDLERS["bulk_retry_stage"] = _handle_bulk_retry_stage
HANDLERS["index_file"] = _drain_index_file
HANDLERS["transcode_proxy"] = _drain_transcode_proxy
HANDLERS["estimate_locations"] = _drain_estimate_locations
HANDLERS["estimate_photo_location"] = _drain_estimate_photo_location


_OWN_KINDS = list(HANDLERS.keys())  # filter so we don't steal ML worker's jobs


def _worker_loop(shutdown: threading.Event, worker_id: int) -> None:
    s = get_settings()
    log.info("worker thread %d started", worker_id)
    try:
        while not shutdown.is_set():
            with SessionLocal() as db:
                job = jobs_mod.claim_one(db, kinds=_OWN_KINDS)
            if job is None:
                shutdown.wait(s.worker.idle_poll_seconds)
                continue

            handler = HANDLERS.get(job.kind)
            if handler is None:
                with SessionLocal() as db:
                    jobs_mod.fail(db, job.id, f"no handler for kind={job.kind!r}")
                continue

            # Inject the job id so long-running handlers can update
            # progress counters and poll for cancellation. Older handlers
            # that don't read _job_id just ignore the extra key.
            payload = jobs_mod.load_payload(job)
            payload["_job_id"] = job.id
            try:
                with SessionLocal() as db:
                    handler(db, payload)
                with SessionLocal() as db:
                    jobs_mod.complete(db, job.id)
            except Exception as e:
                if jobs_mod.is_transient_lock(e):
                    log.warning(
                        "job %d (%s) hit a transient DB lock — requeueing",
                        job.id, job.kind,
                    )
                    with SessionLocal() as db:
                        jobs_mod.fail(db, job.id, str(e), requeue=True)
                else:
                    log.exception("job %d (%s) failed", job.id, job.kind)
                    with SessionLocal() as db:
                        jobs_mod.fail(db, job.id, str(e))
    finally:
        # Clean up the per-thread exiftool subprocess so it doesn't
        # outlive the worker on shutdown.
        exiftool_pool.shutdown_thread()
        log.info("worker thread %d stopped", worker_id)


def _sweeper(shutdown: threading.Event) -> None:
    """Periodically reclaim stale 'running' jobs from crashed workers."""
    s = get_settings()
    interval = max(60, s.worker.job_lease_seconds // 2)
    while not shutdown.is_set():
        try:
            with SessionLocal() as db:
                n = jobs_mod.reclaim_stale(db, s.worker.job_lease_seconds)
                if n:
                    log.warning("reclaimed %d stale running jobs", n)
        except Exception:
            log.exception("sweeper iteration failed")
        shutdown.wait(interval)


def run(shutdown: threading.Event) -> None:
    """Start N worker threads + sweeper, block until shutdown is set."""
    s = get_settings()
    threads: list[threading.Thread] = []
    for i in range(s.worker.concurrency):
        t = threading.Thread(target=_worker_loop, args=(shutdown, i), daemon=True)
        t.start()
        threads.append(t)
    sw = threading.Thread(target=_sweeper, args=(shutdown,), daemon=True)
    sw.start()
    threads.append(sw)

    shutdown.wait()
    # Threads are daemonic and check shutdown each loop turn; give them a moment.
    for t in threads:
        t.join(timeout=5)

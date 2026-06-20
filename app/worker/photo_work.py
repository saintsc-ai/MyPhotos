"""Photo-unit work queue — helpers + dispatcher loop.

The legacy `jobs` table stores one row per (kind, photo) pair. This
module replaces that with a single row per photo and a JSON `stages`
map walked in fixed order. The two queues coexist during migration:
the legacy dispatcher (worker/dispatcher.py) keeps draining old kinds
while this one services the new photo_work rows.

Public surface:
  STAGE_ORDER          — tuple of stage names walked left-to-right
  enqueue_stage(...)   — request one stage on one photo (UPDATE-only,
                         dedup is implicit because PK is photo_id)
  claim_one(db)        — dispatcher claims the next eligible row
  release(db, row)     — give up the claim (rollback, or stop signal)
  STAGE_HANDLERS       — {stage_name: callable(db, photo)} wired by
                         the dispatcher; thin wrappers around the
                         existing per-stage code paths so this commit
                         doesn't have to reimplement anything.

Cooperative shutdown: the dispatcher checks `_stop` between stages,
not inside them. That keeps stage handlers simple (no flag plumbing)
while still bounding shutdown latency to "current stage's worst
case" — sub-second for most stages, multi-minute only for transcode
of very long videos.
"""

from __future__ import annotations

import json
import logging
import threading
import uuid
from datetime import datetime
from typing import Callable, Optional

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from ..models import Photo, PhotoWork

log = logging.getLogger(__name__)


# Stage names + ordering. Adding a stage means appending to this tuple
# and registering a handler in STAGE_HANDLERS. The dispatcher walks
# stages strictly in this order, so dependencies (transcode needs the
# raw video on disk; classify needs thumbnails) line up automatically.
STAGE_ORDER: tuple[str, ...] = (
    "index",                # SHA + EXIF + thumbnail + GPS extract
    "transcode",            # video → H.264 web proxy (videos only)
    "classify",             # YOLO + CLIP + faces + OCR
    "estimate_location",    # GPS inference from neighbour photos
)

# Stage handlers register themselves by name. Filled in by the
# dispatcher (or by tests) right before .run() — keeps this module
# import-free of the heavy ML / transcode modules so admins can poke
# at the schema without dragging onnxruntime / ffmpeg into the import.
STAGE_HANDLERS: dict[str, Callable[[Session, Photo], None]] = {}


# ---------- queue ops ------------------------------------------------


def enqueue_stage(
    db: Session, photo_id: int, stage: str, *, priority: int = 0,
) -> None:
    """Mark `stage` pending on photo's work row. INSERT-or-UPDATE.

    `priority` only bumps the row up; never downgrades (so an admin's
    high-priority manual request isn't lost when a low-priority
    auto-enqueue arrives a moment later). Caller commits.
    """
    if stage not in STAGE_ORDER:
        raise ValueError(f"unknown stage: {stage!r}")

    row = db.get(PhotoWork, photo_id)
    if row is None:
        row = PhotoWork(
            photo_id=photo_id,
            stages=json.dumps({stage: "pending"}),
            priority=priority,
        )
        db.add(row)
        return

    try:
        stages = json.loads(row.stages or "{}")
    except (ValueError, TypeError):
        stages = {}
    # Only re-pending stages that are missing or already failed. An
    # already-pending or already-ok stage stays as-is (avoids the
    # "user clicked twice" double-work pattern, and idempotent for
    # re-runs of index_file).
    if stages.get(stage) in (None, "failed"):
        stages[stage] = "pending"
        row.stages = json.dumps(stages)
    if priority > row.priority:
        row.priority = priority


def has_pending(stages_json: str) -> bool:
    try:
        stages = json.loads(stages_json or "{}")
    except (ValueError, TypeError):
        return False
    return any(v == "pending" for v in stages.values())


_CLAIM_SQL = text(
    """
    UPDATE photo_work
       SET claim_token = :token,
           claimed_at  = :now,
           attempts    = attempts + 1
     WHERE photo_id = (
        SELECT photo_id FROM photo_work
         WHERE claim_token IS NULL
        ORDER BY priority DESC, photo_id ASC
         LIMIT 1
     )
    """
)


def claim_one(db: Session) -> Optional[PhotoWork]:
    """Atomically grab one unclaimed row. Returns None when the queue
    is empty.

    SQLite has no SELECT FOR UPDATE, but its single-writer model
    serialises the UPDATE↔SELECT pair so the claim_token round-trip
    is race-free.
    """
    token = str(uuid.uuid4())
    now = datetime.utcnow()
    try:
        db.execute(_CLAIM_SQL, {"token": token, "now": now})
        db.commit()
    except Exception:
        db.rollback()
        return None

    row = db.execute(
        select(PhotoWork).where(PhotoWork.claim_token == token)
    ).scalar_one_or_none()
    return row


def release(db: Session, row: PhotoWork, *, error: Optional[str] = None) -> None:
    """Give up the claim without finishing the row. Used on _stop or
    on whole-row failure (claim still counts toward attempts so the
    sweeper can bail stuck rows after N retries).
    """
    row.claim_token = None
    row.claimed_at = None
    if error:
        row.last_error = error[:1000]
    db.commit()


def finish(db: Session, row: PhotoWork, *, delete: bool = True) -> None:
    """All stages settled. Either drop the row (delete=True, default)
    or keep it for audit (delete=False, leaves stages so the admin
    can see the final state).
    """
    if delete:
        db.delete(row)
    else:
        row.claim_token = None
        row.claimed_at = None
    db.commit()


# ---------- dispatcher loop -----------------------------------------


_stop = threading.Event()


def signal_stop() -> None:
    """Cooperative shutdown — set by the SIGTERM/SIGINT handler."""
    _stop.set()


def run_worker_loop(poll_seconds: float = 2.0) -> None:
    """Single worker thread. Multi-thread = run this N times. Each
    iteration: claim → walk stages with _stop checks between stages
    → finish or release.
    """
    from ..db import SessionLocal

    while not _stop.is_set():
        db = SessionLocal()
        try:
            row = claim_one(db)
            if row is None:
                db.close()
                _stop.wait(timeout=poll_seconds)
                continue

            _process(db, row)
        except Exception:
            log.exception("photo_work loop iteration crashed (continuing)")
            db.rollback()
        finally:
            db.close()


def _process(db: Session, row: PhotoWork) -> None:
    """Walk one row's pending stages. Commits per stage so a crash
    after stage K leaves K-1 ok + K running for the sweeper to
    reclaim later."""
    try:
        stages = json.loads(row.stages or "{}")
    except (ValueError, TypeError):
        stages = {}

    photo = db.get(Photo, row.photo_id)
    if photo is None:
        # Photo deleted out from under us — drop the work row.
        finish(db, row)
        return

    for stage in STAGE_ORDER:
        if _stop.is_set():
            release(db, row)
            return
        if stages.get(stage) != "pending":
            continue
        handler = STAGE_HANDLERS.get(stage)
        if handler is None:
            stages[stage] = "skipped"
            row.stages = json.dumps(stages)
            db.commit()
            continue
        try:
            handler(db, photo)
            stages[stage] = "ok"
        except Exception as e:
            log.exception(
                "photo_work stage %r failed for photo %d", stage, photo.id,
            )
            stages[stage] = "failed"
            row.last_error = (str(e) or e.__class__.__name__)[:1000]
        row.stages = json.dumps(stages)
        db.commit()

    # All stages settled — drop the row.
    finish(db, row)


# ---------- stage handlers (thin wrappers around existing code) -----


def _index_handler(db: Session, photo: Photo) -> None:
    """Re-runs the existing single-file indexer (SHA + EXIF + thumb +
    GPS extract). We pass photo.id through the existing run() so the
    code path is the same one discover_root has been using for two
    years — no behaviour drift between legacy jobs and photo_work.
    """
    from . import index_file
    index_file.run(db, {"photo_id": photo.id})


def _transcode_handler(db: Session, photo: Photo) -> None:
    """Build the web-playable H.264 proxy. Images are skipped silently
    so a single-stage entry doesn't fail the whole row.
    """
    import os
    from ..config import get_settings
    from ..models import Root
    from ..scanner.utils import join_root
    from . import transcode as transcode_mod

    if photo.media_kind != "video":
        return                                  # → 'skipped' by dispatcher

    if (
        photo.proxy_status == "done"
        and photo.sha256
        and transcode_mod.proxy_path(photo.sha256).exists()
    ):
        return                                  # already settled
    if not photo.sha256:
        photo.proxy_status = "failed"
        photo.proxy_error = "no sha256 (file not yet hashed)"
        db.commit()
        return
    root = db.get(Root, photo.root_id)
    src = join_root(root.abs_path, photo.rel_path) if root else None
    if not src or not os.path.exists(src):
        photo.proxy_status = "failed"
        photo.proxy_error = "source file not found"
        db.commit()
        return
    photo.proxy_status = "running"
    photo.proxy_error = None
    db.commit()
    res = transcode_mod.generate_proxy(src, photo.sha256)
    refreshed = db.get(Photo, photo.id)
    if refreshed is None:
        return
    refreshed.proxy_status = res.status
    refreshed.proxy_error = res.error
    db.commit()
    if res.status == "done":
        try:
            cap_gb = get_settings().video.proxy_cache_max_gb
            transcode_mod.enforce_cache_cap(int(cap_gb * 1024 ** 3))
        except Exception:
            log.exception("proxy cache cap enforcement failed (non-fatal)")


def _classify_handler(db: Session, photo: Photo) -> None:
    """ML classification lives on the dedicated ml-worker process, so
    the photo_work dispatcher just hands the photo off via the legacy
    classify_ml job. The ml-worker re-reads the photo's per-stage
    status columns when it picks the job up, so the work that
    actually runs there is the same as before — this is purely a
    queue plumbing change.
    """
    from . import jobs as jobs_mod
    jobs_mod.enqueue_unique_for_photo(
        db, kind="classify_ml", photo_id=photo.id, priority=4,
    )
    db.commit()


def _estimate_location_handler(db: Session, photo: Photo) -> None:
    """Same algorithm as the legacy estimate_photo_location job, just
    called inline. apply_estimate is idempotent (overwrites only its
    own 'estimated' rows) so a re-run after a queue reset is safe.
    """
    from . import location_estimator as estimator
    if photo.taken_at is None:
        return
    est = estimator.estimate_for_photo(
        db, photo, threshold_seconds=estimator.DEFAULT_THRESHOLD_SECONDS,
    )
    if est is None:
        return
    estimator.apply_estimate(db, photo, est)
    db.commit()


def register_handlers() -> None:
    """Wire the stage handlers into STAGE_HANDLERS. Called from
    worker/main.py at startup — keeps import side effects out of
    `import photo_work` so admins running `alembic upgrade head` or
    tests don't drag the entire worker tree.
    """
    STAGE_HANDLERS["index"] = _index_handler
    STAGE_HANDLERS["transcode"] = _transcode_handler
    STAGE_HANDLERS["classify"] = _classify_handler
    STAGE_HANDLERS["estimate_location"] = _estimate_location_handler

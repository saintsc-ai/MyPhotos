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

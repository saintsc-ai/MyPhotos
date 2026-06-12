"""Job queue helpers.

Claim pattern for SQLite (no SKIP LOCKED):
  1. Generate a UUID4 claim_token.
  2. UPDATE jobs SET status='running', claim_token=?, started_at=NOW
     WHERE id IN (SELECT id FROM jobs
                  WHERE status='queued'
                  ORDER BY priority DESC, id ASC LIMIT 1)
  3. SELECT one row WHERE claim_token=?   — that's our job.

A separate `reclaim_stale` sweep moves rows whose started_at exceeds the
lease back to 'queued' so a crashed worker can't strand them.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import delete, select, text, update
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from ..models import Job


def _execute_commit_retry(db: Session, stmt, *, attempts: int = 6, base: float = 0.5) -> None:
    """Run one write statement + commit, retrying on SQLite 'database is
    locked'. The job-lifecycle writes are single idempotent UPDATEs, so
    re-running after a rolled-back lock is safe — this keeps the dispatcher
    from cascade-failing (job left 'running', then even marking it failed
    fails) under heavy concurrent-write batches (big OCR/ML runs)."""
    for i in range(attempts):
        try:
            db.execute(stmt)
            db.commit()
            return
        except OperationalError as e:
            db.rollback()
            if "locked" in str(e).lower() and i < attempts - 1:
                time.sleep(base * (i + 1))
                continue
            raise

log = logging.getLogger(__name__)


def is_transient_lock(exc: BaseException) -> bool:
    """True when `exc` is a transient SQLite 'database is locked' error.

    WAL keeps reads non-blocking, but two writers upgrading at once (the two
    ML threads + the indexing worker) can still hit SQLITE_BUSY that even a
    long busy_timeout won't wait out. Such a job should be REQUEUED, not
    marked failed — it'll succeed on a retry once the lock clears.
    """
    return isinstance(exc, OperationalError) and "locked" in str(exc).lower()


def purge_done_older_than(db: Session, days: int) -> int:
    """Delete completed jobs whose `finished_at` is older than `days`.

    Done rows are history the workers never read again; left unpurged the
    jobs table grows without bound (one row per processed file × stage).
    `days <= 0` disables and returns 0. Only rows with a finished_at are
    touched, so a just-completed job is never at risk.
    """
    if days <= 0:
        return 0
    cutoff = datetime.utcnow() - timedelta(days=days)
    res = db.execute(
        delete(Job).where(
            Job.status == "done",
            Job.finished_at.is_not(None),
            Job.finished_at < cutoff,
        )
    )
    db.commit()
    return res.rowcount or 0


def enqueue(
    db: Session,
    kind: str,
    payload: dict[str, Any],
    priority: int = 0,
    photo_id: int | None = None,
) -> int:
    """Insert a job. Caller commits.

    `photo_id` (optional) populates the dedicated column so per-photo
    dedup (see enqueue_unique_for_photo) can find live rows without
    parsing JSON. Pass it whenever the payload's logical key is a
    photo — the column stays NULL for queue kinds whose unit of work
    isn't a single photo (discover_root, dedup_cleanup, …).
    """
    job = Job(
        kind=kind,
        payload=json.dumps(payload, ensure_ascii=False),
        priority=priority,
        status="queued",
        photo_id=photo_id,
    )
    db.add(job)
    db.flush()
    return job.id


def enqueue_unique_for_photo(
    db: Session,
    kind: str,
    photo_id: int,
    priority: int = 0,
    extra_payload: dict[str, Any] | None = None,
) -> int:
    """Insert one job per (kind, photo_id) — coalesce with the live one
    if it's still queued or running.

    Why: hitting "분류 시작" twice for the same photo (or the indexing
    worker auto-enqueueing classify_ml right after thumb completion
    while a manual run is still mid-queue) used to drop two identical
    classify_ml rows into the queue. The worker re-reads the photo's
    per-stage status columns when it picks each job up, so the second
    run is effectively a no-op — but the donut and queue table both
    inflate, hiding real progress from the admin.

    Logic:
      1. SELECT id from jobs where (kind, photo_id) matches AND status
         is queued/running.
            • status='done'   → history, never reused — a fresh request
              for the same photo gets its own row.
            • status='failed' → kept explicit; user retries by clicking
              again, the new attempt is its own row (so the previous
              failure stays visible in 최근 실패한 잡).
            • status='queued'/'running' → live; this is the row we
              coalesce into.
      2. If found, return its id WITHOUT inserting. The caller has
         already toggled the per-stage status columns on the photo,
         so when the existing job is picked up (or its currently-
         running stages loop checks them on next iter) the new work
         lands automatically.
      3. Otherwise, call enqueue() with photo_id populated.

    Race notes:
      Concurrent admin clicks can theoretically squeak past the
      SELECT before each other's INSERT lands. In practice
      enqueue_classify processes photos serially in one HTTP request,
      and the indexing worker is single-threaded per kind, so the
      window is tiny. SQLite's writer-serialised model closes it
      entirely; on MariaDB/PG the worst case is one extra duplicate
      that the worker treats as a no-op (same idempotent behaviour we
      had before this helper existed). Not worth a SELECT FOR UPDATE
      for the noise it adds.
    """
    existing_id = db.execute(
        select(Job.id)
        .where(
            Job.kind == kind,
            Job.photo_id == photo_id,
            Job.status.in_(("queued", "running")),
        )
        .order_by(Job.id.asc())
        .limit(1)
    ).scalar()
    if existing_id is not None:
        return existing_id
    payload = {"photo_id": photo_id}
    if extra_payload:
        payload.update(extra_payload)
    return enqueue(db, kind=kind, payload=payload, priority=priority, photo_id=photo_id)


def enqueue_many(
    db: Session, kind: str, payloads: list[dict[str, Any]], priority: int = 0
) -> int:
    """Bulk insert. Returns count. Caller commits."""
    if not payloads:
        return 0
    db.bulk_save_objects(
        [
            Job(
                kind=kind,
                payload=json.dumps(p, ensure_ascii=False),
                priority=priority,
                status="queued",
            )
            for p in payloads
        ]
    )
    return len(payloads)


def claim_one(db: Session, kinds: list[str] | None = None) -> Job | None:
    """Claim a single queued job atomically. Returns the Job or None.

    `kinds` restricts which job kinds this caller picks up — used to keep
    the regular worker and the ML worker from stealing each other's jobs.
    None means "any kind".

    The UPDATE+SELECT pattern is safe across multiple workers because
    each one mints a unique claim_token. We peek with a SELECT before
    issuing the UPDATE so idle workers don't pile up writer-lock attempts
    on the jobs table (WAL makes SELECT concurrent; UPDATE serialises).

    Transient "database is locked" errors are absorbed and reported as
    None — the caller's poll loop will retry on the next tick.
    """
    # Cheap pre-check — skip the writer-lock attempt when nothing's queued.
    if kinds:
        peek_placeholders = ", ".join(f":k{i}" for i in range(len(kinds)))
        peek_params = {f"k{i}": k for i, k in enumerate(kinds)}
        peek_sql = (
            f"SELECT 1 FROM jobs WHERE status = 'queued' "
            f"AND kind IN ({peek_placeholders}) LIMIT 1"
        )
    else:
        peek_params = {}
        peek_sql = "SELECT 1 FROM jobs WHERE status = 'queued' LIMIT 1"
    try:
        if db.execute(text(peek_sql), peek_params).first() is None:
            return None
    except OperationalError as e:
        if "locked" in str(e).lower():
            log.debug("claim_one: db locked during peek, retry next tick")
            return None
        raise

    token = str(uuid.uuid4())
    if kinds:
        placeholders = ", ".join(f":k{i}" for i in range(len(kinds)))
        params = {"token": token}
        params.update({f"k{i}": k for i, k in enumerate(kinds)})
        sql = f"""
            UPDATE jobs
               SET status      = 'running',
                   claim_token = :token,
                   started_at  = CURRENT_TIMESTAMP,
                   attempts    = attempts + 1
             WHERE id = (
                 SELECT id FROM jobs
                  WHERE status = 'queued' AND kind IN ({placeholders})
                  ORDER BY priority DESC, id ASC
                  LIMIT 1
             )
        """
    else:
        params = {"token": token}
        sql = """
            UPDATE jobs
               SET status      = 'running',
                   claim_token = :token,
                   started_at  = CURRENT_TIMESTAMP,
                   attempts    = attempts + 1
             WHERE id = (
                 SELECT id FROM jobs
                  WHERE status = 'queued'
                  ORDER BY priority DESC, id ASC
                  LIMIT 1
             )
        """
    try:
        result = db.execute(text(sql), params)
        db.commit()
    except OperationalError as e:
        db.rollback()
        if "locked" in str(e).lower():
            log.warning("claim_one: db locked during claim, retry next tick")
            return None
        raise
    if result.rowcount == 0:
        return None
    return db.execute(select(Job).where(Job.claim_token == token)).scalar_one_or_none()


def complete(db: Session, job_id: int) -> None:
    # If the job was cancelled mid-run, don't overwrite that with 'done' —
    # the worker may finish its current chunk after the cancel mark lands.
    _execute_commit_retry(
        db,
        update(Job)
        .where(Job.id == job_id, Job.status == "running")
        .values(status="done", finished_at=datetime.utcnow(), claim_token=None),
    )


def set_progress(
    db: Session, job_id: int, *, done: int | None = None, total: int | None = None,
) -> None:
    """Update a job's progress counters. Either field can be omitted to
    leave it unchanged. Caller-independent — opens its own commit so the
    UI sees fresh numbers without waiting for the handler's transaction.
    """
    values: dict = {}
    if done is not None:
        values["progress_done"] = int(done)
    if total is not None:
        values["progress_total"] = int(total)
    if not values:
        return
    _execute_commit_retry(db, update(Job).where(Job.id == job_id).values(**values))


def is_cancelled(db: Session, job_id: int) -> bool:
    """Polled by long-running handlers between chunks so an admin's
    cancel request can break the loop without waiting for the whole job."""
    row = db.execute(
        select(Job.status).where(Job.id == job_id)
    ).scalar_one_or_none()
    return row == "cancelled"


def fail(db: Session, job_id: int, error: str, *, requeue: bool = False) -> None:
    """Mark job failed. If requeue=True, send it back to 'queued' (e.g. transient error)."""
    _execute_commit_retry(
        db,
        update(Job)
        .where(Job.id == job_id)
        .values(
            status="queued" if requeue else "failed",
            finished_at=None if requeue else datetime.utcnow(),
            claim_token=None,
            last_error=error[:4000],
        ),
    )


def reclaim_stale(db: Session, lease_seconds: int) -> int:
    """Move 'running' jobs whose lease expired back to 'queued'. Returns count.

    Tolerates "database is locked" — a long indexing batch may hold the
    writer lock past busy_timeout. The next sweep tick will retry.
    """
    cutoff = datetime.utcnow() - timedelta(seconds=lease_seconds)
    try:
        result = db.execute(
            update(Job)
            .where(Job.status == "running", Job.started_at < cutoff)
            .values(status="queued", claim_token=None, started_at=None)
        )
        db.commit()
    except OperationalError as e:
        db.rollback()
        if "locked" in str(e).lower():
            log.debug("reclaim_stale: db locked, retry next sweep")
            return 0
        raise
    return result.rowcount or 0


def load_payload(job: Job) -> dict[str, Any]:
    return json.loads(job.payload)

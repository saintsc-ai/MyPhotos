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
import uuid
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select, text, update
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from ..models import Job

log = logging.getLogger(__name__)


def enqueue(db: Session, kind: str, payload: dict[str, Any], priority: int = 0) -> int:
    """Insert a job. Caller commits."""
    job = Job(
        kind=kind,
        payload=json.dumps(payload, ensure_ascii=False),
        priority=priority,
        status="queued",
    )
    db.add(job)
    db.flush()
    return job.id


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
    db.execute(
        update(Job)
        .where(Job.id == job_id, Job.status == "running")
        .values(status="done", finished_at=datetime.utcnow(), claim_token=None)
    )
    db.commit()


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
    db.execute(update(Job).where(Job.id == job_id).values(**values))
    db.commit()


def is_cancelled(db: Session, job_id: int) -> bool:
    """Polled by long-running handlers between chunks so an admin's
    cancel request can break the loop without waiting for the whole job."""
    row = db.execute(
        select(Job.status).where(Job.id == job_id)
    ).scalar_one_or_none()
    return row == "cancelled"


def fail(db: Session, job_id: int, error: str, *, requeue: bool = False) -> None:
    """Mark job failed. If requeue=True, send it back to 'queued' (e.g. transient error)."""
    db.execute(
        update(Job)
        .where(Job.id == job_id)
        .values(
            status="queued" if requeue else "failed",
            finished_at=None if requeue else datetime.utcnow(),
            claim_token=None,
            last_error=error[:4000],
        )
    )
    db.commit()


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

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
import uuid
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select, text, update
from sqlalchemy.orm import Session

from ..models import Job


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


def claim_one(db: Session) -> Job | None:
    """Claim a single queued job atomically. Returns the Job or None.

    The UPDATE+SELECT pattern is safe across multiple workers because
    each one mints a unique claim_token.
    """
    token = str(uuid.uuid4())
    # SQLite supports UPDATE ... WHERE id = (SELECT ... LIMIT 1)
    result = db.execute(
        text(
            """
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
        ),
        {"token": token},
    )
    db.commit()
    if result.rowcount == 0:
        return None
    return db.execute(select(Job).where(Job.claim_token == token)).scalar_one_or_none()


def complete(db: Session, job_id: int) -> None:
    db.execute(
        update(Job)
        .where(Job.id == job_id)
        .values(status="done", finished_at=datetime.utcnow(), claim_token=None)
    )
    db.commit()


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
    """Move 'running' jobs whose lease expired back to 'queued'. Returns count."""
    cutoff = datetime.utcnow() - timedelta(seconds=lease_seconds)
    result = db.execute(
        update(Job)
        .where(Job.status == "running", Job.started_at < cutoff)
        .values(status="queued", claim_token=None, started_at=None)
    )
    db.commit()
    return result.rowcount or 0


def load_payload(job: Job) -> dict[str, Any]:
    return json.loads(job.payload)

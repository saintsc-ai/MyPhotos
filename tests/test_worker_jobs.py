"""Unit tests for app.worker.jobs queue helpers."""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Job
from app.worker.jobs import purge_done_older_than


def _job(status: str, finished_days_ago: float | None = None) -> Job:
    fin = None
    if finished_days_ago is not None:
        fin = datetime.utcnow() - timedelta(days=finished_days_ago)
    return Job(kind="detect_faces", payload="{}", status=status, finished_at=fin)


def test_purge_done_older_than_drops_only_old_done(db: Session):
    old = _job("done", finished_days_ago=5)       # past 3-day window → purged
    recent = _job("done", finished_days_ago=0.5)  # within window → kept
    queued = _job("queued")                       # not done → kept
    failed = _job("failed", finished_days_ago=10) # not done → kept
    db.add_all([old, recent, queued, failed])
    db.commit()

    n = purge_done_older_than(db, days=3)
    assert n == 1

    left = db.execute(select(Job.status, Job.finished_at)).all()
    statuses = sorted(s for s, _ in left)
    assert statuses == ["done", "failed", "queued"]   # the 5-day done is gone
    # The surviving done row is the recent one.
    done_rows = [f for s, f in left if s == "done"]
    assert len(done_rows) == 1


def test_purge_done_older_than_disabled_when_zero(db: Session):
    db.add(_job("done", finished_days_ago=99))
    db.commit()
    assert purge_done_older_than(db, days=0) == 0          # disabled
    assert db.execute(select(Job)).scalars().first() is not None

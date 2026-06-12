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


def test_is_transient_lock():
    from sqlalchemy.exc import OperationalError

    from app.worker.jobs import is_transient_lock

    locked = OperationalError("DELETE FROM photo_auto_tags", {},
                              Exception("database is locked"))
    other = OperationalError("SELECT 1", {}, Exception("no such table: x"))
    assert is_transient_lock(locked) is True
    assert is_transient_lock(other) is False
    assert is_transient_lock(ValueError("locked")) is False   # not an OperationalError


# ---- enqueue_unique_for_photo dedup ----------------------------------

def _count_classify_ml(db: Session, photo_id: int) -> int:
    return db.execute(
        select(Job).where(Job.kind == "classify_ml", Job.photo_id == photo_id)
    ).scalars().all().__len__()


def test_enqueue_unique_for_photo_skips_duplicate_queued(db: Session):
    """Two enqueues for the same photo while the first is still
    queued → only one job row exists."""
    from app.worker.jobs import enqueue_unique_for_photo

    first = enqueue_unique_for_photo(db, kind="classify_ml", photo_id=42, priority=3)
    db.commit()
    second = enqueue_unique_for_photo(db, kind="classify_ml", photo_id=42, priority=3)
    db.commit()

    assert second == first                           # returned the existing id
    assert _count_classify_ml(db, 42) == 1            # still one row


def test_enqueue_unique_for_photo_creates_per_photo_row(db: Session):
    """Different photo ids never collapse — each gets its own row."""
    from app.worker.jobs import enqueue_unique_for_photo

    enqueue_unique_for_photo(db, kind="classify_ml", photo_id=1, priority=3)
    enqueue_unique_for_photo(db, kind="classify_ml", photo_id=2, priority=3)
    db.commit()

    assert _count_classify_ml(db, 1) == 1
    assert _count_classify_ml(db, 2) == 1
    assert db.execute(select(Job).where(Job.kind == "classify_ml")).scalars().all().__len__() == 2


def test_enqueue_unique_for_photo_done_does_not_block_new(db: Session):
    """A completed (status='done') job for the same photo is history;
    a fresh enqueue must still create a new row so the user can
    re-classify."""
    from app.worker.jobs import enqueue_unique_for_photo

    jid = enqueue_unique_for_photo(db, kind="classify_ml", photo_id=7, priority=3)
    db.execute(
        Job.__table__.update().where(Job.id == jid).values(
            status="done", finished_at=datetime.utcnow()
        )
    )
    db.commit()

    new_id = enqueue_unique_for_photo(db, kind="classify_ml", photo_id=7, priority=3)
    db.commit()
    assert new_id != jid                              # new job
    assert _count_classify_ml(db, 7) == 2             # done + fresh queued


def test_enqueue_unique_for_photo_failed_does_not_block_new(db: Session):
    """A failed job is left visible in the queue for the admin; a
    new enqueue creates a fresh attempt rather than coalescing into
    the failed row."""
    from app.worker.jobs import enqueue_unique_for_photo

    jid = enqueue_unique_for_photo(db, kind="classify_ml", photo_id=9, priority=3)
    db.execute(
        Job.__table__.update().where(Job.id == jid).values(
            status="failed", last_error="synthetic"
        )
    )
    db.commit()

    new_id = enqueue_unique_for_photo(db, kind="classify_ml", photo_id=9, priority=3)
    db.commit()
    assert new_id != jid
    assert _count_classify_ml(db, 9) == 2


def test_enqueue_unique_for_photo_coalesces_into_running(db: Session):
    """A `running` job (worker has picked it up but hasn't finished
    yet) absorbs a new request — the worker re-reads photo.*_status
    on each stage iter, so the new stage lands without an extra row."""
    from app.worker.jobs import enqueue_unique_for_photo

    jid = enqueue_unique_for_photo(db, kind="classify_ml", photo_id=11, priority=3)
    db.execute(
        Job.__table__.update().where(Job.id == jid).values(
            status="running", claim_token="test-token"
        )
    )
    db.commit()

    second = enqueue_unique_for_photo(db, kind="classify_ml", photo_id=11, priority=3)
    db.commit()
    assert second == jid                              # coalesced into running
    assert _count_classify_ml(db, 11) == 1

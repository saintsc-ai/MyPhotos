"""discover_root queue hygiene: the periodic due-scan tick must not pile
up duplicate scans for a root, and a long-running job's lease must be
renewable so the sweeper doesn't requeue it mid-run."""

from __future__ import annotations

import json
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.models import Job
from app.worker import jobs as jobs_mod

from tests.conftest import make_root


def _discover_jobs(db: Session, root_id: int | None = None) -> list[Job]:
    rows = db.execute(
        select(Job).where(Job.kind == "discover_root").order_by(Job.id)
    ).scalars().all()
    if root_id is None:
        return rows
    return [j for j in rows if json.loads(j.payload)["root_id"] == root_id]


def _set_status(db: Session, job_id: int, status: str) -> None:
    db.execute(Job.__table__.update().where(Job.id == job_id).values(status=status))
    db.commit()


def test_tick_does_not_stack_scans(engine, db: Session, monkeypatch):
    """Regression: an unfinished scan leaves last_full_scan stale, and
    every 10-min tick used to add another discover_root (8k+ on the NAS)."""
    from app.worker import main as worker_main

    r1 = make_root(db, label="a", abs_path="/tmp/a")
    r2 = make_root(db, label="b", abs_path="/tmp/b")
    db.commit()
    monkeypatch.setattr(worker_main, "SessionLocal", sessionmaker(bind=engine))

    for _ in range(5):
        worker_main._enqueue_due_root_scans()
    db.expire_all()
    assert len(_discover_jobs(db, r1.id)) == 1
    assert len(_discover_jobs(db, r2.id)) == 1

    # Once r1's scan is running (still unfinished), ticks still skip it.
    _set_status(db, _discover_jobs(db, r1.id)[0].id, "running")
    for _ in range(3):
        worker_main._enqueue_due_root_scans()
    db.expire_all()
    assert len(_discover_jobs(db, r1.id)) == 1
    assert len(_discover_jobs(db, r2.id)) == 1

    # A finished scan no longer counts — the next due tick queues anew.
    _set_status(db, _discover_jobs(db, r1.id)[0].id, "done")
    worker_main._enqueue_due_root_scans()
    db.expire_all()
    assert [j.status for j in _discover_jobs(db, r1.id)] == ["done", "queued"]


def test_enqueue_discover_root_coalesces_queued_and_bumps_priority(db: Session):
    r = make_root(db)
    first, created = jobs_mod.enqueue_discover_root(db, r.id, priority=10)
    db.commit()
    assert created
    again, created = jobs_mod.enqueue_discover_root(db, r.id, priority=20)
    db.commit()
    assert (again, created) == (first, False)
    assert db.get(Job, first).priority == 20
    assert len(_discover_jobs(db)) == 1


def test_enqueue_discover_root_running_behaviour(db: Session):
    """Uploads/manual scans still queue a follow-up behind a running scan
    (it may have walked past the new files); the tick does not."""
    r = make_root(db)
    jid, _ = jobs_mod.enqueue_discover_root(db, r.id, priority=10)
    db.commit()
    _set_status(db, jid, "running")

    same, created = jobs_mod.enqueue_discover_root(db, r.id, priority=10, skip_if_running=True)
    assert (same, created) == (jid, False)

    follow, created = jobs_mod.enqueue_discover_root(db, r.id, priority=15)
    db.commit()
    assert created and follow != jid
    # ...but only one follow-up: the next call coalesces into it.
    again, created = jobs_mod.enqueue_discover_root(db, r.id, priority=15)
    assert (again, created) == (follow, False)


def test_enqueue_discover_root_limit_scans(db: Session):
    """A limited scan doesn't stand in for a full scan."""
    r = make_root(db)
    lim, _ = jobs_mod.enqueue_discover_root(db, r.id, priority=20, limit=100)
    db.commit()
    assert json.loads(db.get(Job, lim).payload) == {"root_id": r.id, "limit": 100}
    full, created = jobs_mod.enqueue_discover_root(db, r.id, priority=10)
    assert created and full != lim


def test_renew_lease_keeps_running_job_from_being_reclaimed(db: Session):
    old = datetime.utcnow() - timedelta(hours=2)
    live = Job(kind="discover_root", payload="{}", status="running", started_at=old)
    dead = Job(kind="discover_root", payload="{}", status="running", started_at=old)
    db.add_all([live, dead])
    db.commit()

    jobs_mod.renew_lease(db, live.id)
    assert jobs_mod.reclaim_stale(db, lease_seconds=600) == 1
    db.expire_all()
    assert db.get(Job, live.id).status == "running"
    assert db.get(Job, dead.id).status == "queued"


def test_renew_lease_ignores_non_running(db: Session):
    j = Job(kind="discover_root", payload="{}", status="done", started_at=None)
    db.add(j)
    db.commit()
    jobs_mod.renew_lease(db, j.id)
    db.expire_all()
    assert db.get(Job, j.id).started_at is None

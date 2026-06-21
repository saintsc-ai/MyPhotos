"""Worker entry point.

Runs the dispatcher (N threads consuming the SQLite job queue) plus a
stale-job sweeper. Discovery is triggered by the API (POST /admin/roots/{id}/scan)
or by the daily APScheduler tick.

Run with: python -m app.worker.main
"""

from __future__ import annotations

import logging
import signal
import sys
import threading
from datetime import datetime, timedelta

from apscheduler.schedulers.background import BackgroundScheduler
from sqlalchemy import select, text

from ..config import get_settings
from ..db import SessionLocal, engine
from ..external import exiftool_path, ffmpeg_path
from ..models import Root
from ..paths import LOGS_DIR, ensure_runtime_dirs
from . import dispatcher
from . import jobs as jobs_mod
from . import photo_work as photo_work_mod


def _configure_logging() -> None:
    settings = get_settings()
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=settings.logging.level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
    )


_shutdown = threading.Event()


def _install_signal_handlers() -> None:
    def _handler(signum, _frame):
        logging.getLogger(__name__).info("received signal %s, shutting down", signum)
        _shutdown.set()
        # The new photo_work dispatcher checks its own flag between
        # stages so SIGTERM unblocks it mid-row. Without this, a long
        # transcode stage could keep systemd waiting until the
        # TimeoutStopSec axe falls.
        photo_work_mod.signal_stop()

    signal.signal(signal.SIGINT, _handler)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, _handler)


def _enqueue_due_root_scans() -> None:
    """Daily tick: for each enabled root, enqueue discover_root if its
    interval has elapsed (or it's never been scanned)."""
    from datetime import datetime, timedelta

    with SessionLocal() as db:
        roots = db.execute(select(Root).where(Root.enabled.is_(True))).scalars().all()
        for root in roots:
            due = root.last_full_scan is None or (
                root.last_full_scan
                < datetime.utcnow() - timedelta(seconds=root.scan_interval)
            )
            if due:
                jobs_mod.enqueue(
                    db, kind="discover_root", payload={"root_id": root.id}, priority=10
                )
        db.commit()


def _purge_old_audit_log() -> None:
    """Daily tick: drop audit_log rows older than 90 days. Keeps the
    table small enough that the admin Activity Log endpoint stays
    snappy without manual cleanup.
    """
    from .. import audit as audit_helper

    log = logging.getLogger("worker.audit")
    with SessionLocal() as db:
        try:
            n = audit_helper.purge_older_than_days(db, days=90)
            if n:
                log.info("audit_log: purged %d rows older than 90 days", n)
        except Exception:
            log.exception("audit_log purge failed (non-fatal)")


def _purge_done_jobs() -> None:
    """Daily tick: drop completed jobs older than
    worker.done_job_retention_days so the jobs table doesn't grow without
    bound. 0 disables."""
    days = get_settings().worker.done_job_retention_days
    if days <= 0:
        return
    log = logging.getLogger("worker.jobs")
    with SessionLocal() as db:
        try:
            n = jobs_mod.purge_done_older_than(db, days)
            if n:
                log.info("jobs: purged %d done jobs older than %dd", n, days)
        except Exception:
            log.exception("done-job purge failed (non-fatal)")


def _auto_dedup_cleanup() -> None:
    """Periodic tick (only scheduled when [dedup] auto_cleanup is on):
    enqueue a dedup_cleanup job that keeps the earliest copy of each
    duplicate group and trashes the rest. Skips if one is already
    queued/running so ticks can't pile up on a slow sweep."""
    from ..models import Job, User

    log = logging.getLogger("worker.dedup")
    with SessionLocal() as db:
        active = db.execute(
            select(Job.id).where(
                Job.kind == "dedup_cleanup",
                Job.status.in_(("queued", "running")),
            ).limit(1)
        ).first()
        if active is not None:
            return
        actor = db.execute(
            select(User).where(User.is_admin.is_(True)).order_by(User.id).limit(1)
        ).scalar_one_or_none()
        if actor is None:
            log.warning("auto dedup: no admin user to attribute cleanup to; skipping")
            return
        jobs_mod.enqueue(db, kind="dedup_cleanup",
                         payload={"user_id": actor.id}, priority=3)
        db.commit()
        log.info("auto dedup_cleanup enqueued (actor=%d)", actor.id)


def _reclaim_stale_photo_work() -> None:
    """Periodic tick: release photo_work claims whose claimed_at is
    older than the worker's job lease window. Without this, a
    crashed / SIGKILL'd worker leaves its claim_token set forever
    and that photo's stages never advance."""
    lease = max(60, get_settings().worker.job_lease_seconds)
    log = logging.getLogger("worker.photo_work_sweep")
    with SessionLocal() as db:
        try:
            n = photo_work_mod.reclaim_stale(db, lease)
            if n:
                log.warning("reclaimed %d stale photo_work claims", n)
        except Exception:
            log.exception("photo_work sweeper iteration failed")


def _purge_stale_uploads_pending() -> None:
    """Daily tick: drop uploads_pending rows older than 7 days.

    Rows accumulate when an uploaded file never gets indexed (write
    failed silently, file moved/deleted before the scanner ran, or the
    file's extension isn't in the indexable allowlist). After a week we
    assume it's not going to land — keeping pending rows around forever
    would just confuse later re-uploads at the same path.
    """
    from datetime import datetime, timedelta
    from ..models import UploadPending

    log = logging.getLogger("worker.uploads")
    with SessionLocal() as db:
        try:
            cutoff = datetime.utcnow() - timedelta(days=7)
            rows = db.execute(
                select(UploadPending).where(UploadPending.created_at < cutoff)
            ).scalars().all()
            for r in rows:
                db.delete(r)
            db.commit()
            if rows:
                log.info("uploads_pending: purged %d stale rows", len(rows))
        except Exception:
            log.exception("uploads_pending purge failed (non-fatal)")


def main() -> int:
    ensure_runtime_dirs()
    _configure_logging()
    _install_signal_handlers()

    log = logging.getLogger("worker")
    settings = get_settings()
    log.info("worker starting (concurrency=%d)", settings.worker.concurrency)

    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        log.info("db connection ok")
    except Exception:
        log.exception("db connection failed; exiting")
        return 1

    # Eagerly probe external tools so availability is logged at startup
    # instead of waiting for the first job that needs them.
    et = exiftool_path()
    ff = ffmpeg_path()
    log.info("external tools: exiftool=%s ffmpeg=%s", et or "MISSING", ff or "MISSING")

    # Periodic root-scan trigger + daily audit_log retention sweep.
    scheduler = BackgroundScheduler(daemon=True)
    scheduler.add_job(_enqueue_due_root_scans, "interval", minutes=10, id="root_scan_tick")
    scheduler.add_job(_purge_old_audit_log, "interval", hours=24, id="audit_purge")
    scheduler.add_job(_purge_stale_uploads_pending, "interval", hours=24, id="uploads_pending_purge")
    # Trim completed jobs (status='done') past the retention window so the
    # jobs table stays bounded. First run shortly after start clears the
    # existing backlog without waiting a full day.
    scheduler.add_job(
        _purge_done_jobs, "interval", hours=24, id="done_jobs_purge",
        next_run_time=datetime.now() + timedelta(seconds=60),
    )
    if settings.dedup.auto_cleanup:
        hrs = max(1, settings.dedup.auto_cleanup_interval_hours)
        scheduler.add_job(_auto_dedup_cleanup, "interval", hours=hrs, id="auto_dedup")
        log.info("auto dedup cleanup enabled (every %dh)", hrs)
    # photo_work sweeper — reclaim claims left behind by crashed /
    # SIGKILL'd workers. Without this, every hard kill bleeds claims
    # forever and individual photos get permanently stuck. Lease
    # window matches the legacy dispatcher's 600s default; runs
    # every 5 min so a stuck photo unblocks within ~10 min worst case.
    scheduler.add_job(
        _reclaim_stale_photo_work, "interval", minutes=5, id="photo_work_sweep",
        next_run_time=datetime.now() + timedelta(seconds=30),
    )
    scheduler.start()

    # Parallel queue (photo_work). Same thread budget as the legacy
    # dispatcher — Phase 3 routes new discover/index/estimate work
    # here so the throughput needs to match. The legacy dispatcher
    # naturally winds down as fewer kinds enqueue into it; once it's
    # quiet we can drop its thread count.
    photo_work_mod.register_handlers()
    pw_threads: list[threading.Thread] = []
    pw_count = max(1, int(settings.worker.concurrency))
    for i in range(pw_count):
        t = threading.Thread(
            target=photo_work_mod.run_worker_loop,
            kwargs={"poll_seconds": 2.0},
            name=f"photo_work_dispatcher-{i}",
            daemon=True,
        )
        t.start()
        pw_threads.append(t)
    log.info("photo_work dispatcher threads started (%d workers)", pw_count)

    try:
        dispatcher.run(_shutdown)
    finally:
        scheduler.shutdown(wait=False)
        # Give the photo_work threads a beat to notice _stop and
        # release their current claims before the process exits.
        photo_work_mod.signal_stop()
        for t in pw_threads:
            t.join(timeout=10)

    log.info("worker stopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

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
import time
from typing import Callable

from sqlalchemy import select

from ..config import get_settings
from ..db import SessionLocal
from ..models import Photo, Root
from ..scanner.discover import discover_root
from . import dedup_cleanup as dedup_cleanup_handler
from . import exiftool_pool
from . import index_file as index_file_handler
from . import jobs as jobs_mod
from . import transcode as transcode_mod

log = logging.getLogger(__name__)


Handler = Callable[["Session", dict], None]


HANDLERS: dict[str, Handler] = {
    "index_file": index_file_handler.run,
}


def _handle_discover_root(db, payload: dict) -> None:
    root_id = int(payload["root_id"])
    root = db.get(Root, root_id)
    if root is None:
        log.warning("discover_root: root %d not found", root_id)
        return
    limit = payload.get("limit")
    discover_root(db, root, limit=limit)


def _handle_transcode_proxy(db, payload: dict) -> None:
    """Build a web-playable H.264 proxy for one video (lazy, on demand)."""
    import os

    from ..scanner.utils import join_root

    photo_id = int(payload["photo_id"])
    photo = db.get(Photo, photo_id)
    if photo is None or photo.media_kind != "video":
        return
    if photo.proxy_status == "done" and photo.sha256 \
            and transcode_mod.proxy_path(photo.sha256).exists():
        return
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
    # Re-fetch in case the row changed during the (long) encode.
    photo = db.get(Photo, photo_id)
    if photo is None:
        return
    photo.proxy_status = res.status
    photo.proxy_error = res.error
    db.commit()


HANDLERS["discover_root"] = _handle_discover_root
HANDLERS["dedup_cleanup"] = dedup_cleanup_handler.run
HANDLERS["transcode_proxy"] = _handle_transcode_proxy


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

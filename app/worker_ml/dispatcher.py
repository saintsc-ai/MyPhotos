"""Dispatcher for the ML worker.

Same shape as the regular worker's dispatcher but only picks up the job
kinds this process knows how to handle. ML models are CPU-bound and
their threads contend for the same cores, so we run fewer threads here
than the indexing worker — `ml_concurrency` (default 2).
"""

from __future__ import annotations

import logging
import threading
from typing import Callable

from sqlalchemy.orm import Session

from ..config import get_settings
from ..db import SessionLocal
from ..worker import jobs as jobs_mod
from .jobs import HANDLERS

log = logging.getLogger(__name__)

Handler = Callable[[Session, dict], None]


_OWN_KINDS = list(HANDLERS.keys())


def _worker_loop(shutdown: threading.Event, worker_id: int) -> None:
    s = get_settings()
    idle = s.worker.idle_poll_seconds
    log.info("ml worker thread %d started", worker_id)
    while not shutdown.is_set():
        with SessionLocal() as db:
            job = jobs_mod.claim_one(db, kinds=_OWN_KINDS)
        if job is None:
            shutdown.wait(idle)
            continue

        handler = HANDLERS.get(job.kind)
        if handler is None:
            with SessionLocal() as db:
                jobs_mod.fail(db, job.id, f"no ml handler for kind={job.kind!r}")
            continue

        payload = jobs_mod.load_payload(job)
        try:
            with SessionLocal() as db:
                handler(db, payload)
            with SessionLocal() as db:
                jobs_mod.complete(db, job.id)
        except NotImplementedError as e:
            # Future-stage kind queued early; leave the row failed with a
            # clear message so we don't keep retrying it.
            with SessionLocal() as db:
                jobs_mod.fail(db, job.id, f"not implemented yet: {e}")
        except Exception as e:
            log.exception("ml job %d (%s) failed", job.id, job.kind)
            with SessionLocal() as db:
                jobs_mod.fail(db, job.id, str(e))
    log.info("ml worker thread %d stopped", worker_id)


def run(shutdown: threading.Event) -> None:
    s = get_settings()
    n = getattr(s.worker, "ml_concurrency", None) or 2
    threads: list[threading.Thread] = []
    for i in range(n):
        t = threading.Thread(target=_worker_loop, args=(shutdown, i), daemon=True)
        t.start()
        threads.append(t)
    shutdown.wait()
    for t in threads:
        t.join(timeout=5)

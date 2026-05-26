"""Filesystem watcher — Phase 1 (root-level debounce).

What it does:
  - Subscribes (recursive) to every enabled root with watchdog/inotify.
  - On any event under that root, marks the root "dirty" via the
    RootDebouncer (touch). Ignored: directories/files matching the
    configured ignore patterns, hidden files, common editor tempfiles.
  - A scheduler loop periodically drains roots whose quiet period
    elapsed and enqueues a discover_root job for each, at priority 5
    (above the daily APScheduler tick = 10, below admin trigger = 20).
  - jobs.on-complete bookkeeping: clear_inflight via a small SQL
    polling fallback so a worker crash doesn't strand the flag.

What it intentionally does NOT do (Phase 1):
  - per-path index_path jobs. The root rescan reuses our well-tested
    incremental discover_root logic; per-path comes in Phase 2.
  - subscribing to NFS/SMB-only mounts (inotify can't see those —
    daily full scan stays as the safety net).

Run with: python -m app.watcher.main
"""

from __future__ import annotations

import logging
import os
import signal
import sys
import threading
import time
from typing import Iterable

from sqlalchemy import select
from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

from ..config import get_settings
from ..db import SessionLocal, engine
from ..models import Job, Root
from ..paths import LOGS_DIR, ensure_runtime_dirs
from ..worker import jobs as jobs_mod
from .coalescer import RootDebouncer

log = logging.getLogger(__name__)
_shutdown = threading.Event()


def _configure_logging() -> None:
    s = get_settings()
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=s.logging.level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
    )


def _install_signal_handlers() -> None:
    def _h(signum, _frame):
        log.info("received signal %s, shutting down", signum)
        _shutdown.set()
    signal.signal(signal.SIGINT, _h)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, _h)


# --- Path filtering ---------------------------------------------------------

# Files we never care about (editor tempfiles, OS metadata).
_EXTRA_TEMP_SUFFIXES = (".tmp", ".swp", ".part", ".crdownload", "~")
_EXTRA_TEMP_PREFIXES = (".#", "#")


def _is_ignorable_path(rel_parts: Iterable[str]) -> bool:
    s = get_settings()
    ignored_dirs = set(s.scanner.ignore_dirs)
    ignored_files = set(s.scanner.ignore_files)
    parts = list(rel_parts)
    if not parts:
        return False
    # Any ancestor directory is on the ignore list?
    for p in parts[:-1]:
        if p in ignored_dirs:
            return True
    leaf = parts[-1]
    if leaf in ignored_files:
        return True
    if leaf.startswith("."):
        # hidden file/dir — same logic as scanner.utils
        return True
    if leaf.endswith(_EXTRA_TEMP_SUFFIXES) or leaf.startswith(_EXTRA_TEMP_PREFIXES):
        return True
    return False


# --- watchdog event handler -------------------------------------------------


class _RootEventHandler(FileSystemEventHandler):
    """Single handler shared by all roots, indexed by root_id.

    Why one handler instead of one per root: watchdog allows binding the
    same handler to multiple watches, and we want all dispatch to go
    through the same debouncer instance.
    """

    def __init__(self, root_id: int, root_abs: str, deb: RootDebouncer) -> None:
        super().__init__()
        self.root_id = root_id
        self.root_abs = root_abs.rstrip("/\\")
        self.deb = deb

    def _relevant(self, event_path: str) -> bool:
        # event_path is absolute. Compute relative parts to test against
        # ignore lists. Outside our root → drop (shouldn't happen with
        # recursive watch but guards against symlink escapes).
        try:
            rel = os.path.relpath(event_path, self.root_abs)
        except ValueError:
            return False
        if rel.startswith(".."):
            return False
        parts = rel.replace("\\", "/").split("/")
        if _is_ignorable_path(parts):
            return False
        return True

    def on_any_event(self, event: FileSystemEvent) -> None:
        # We don't differentiate event types here — any change to a
        # relevant path bumps the debounce timer. discover_root figures
        # out whether it's add/modify/delete.
        target = event.src_path
        if not target:
            return
        if not self._relevant(target):
            return
        self.deb.touch(self.root_id)


# --- Observer management ----------------------------------------------------


class WatcherService:
    """Owns the Observer + the per-root watch table.

    Reconcile loop: periodically compares (enabled roots in DB) vs
    (currently watched roots) and adjusts subscriptions. This handles
    admins adding/removing/disabling roots at runtime without a restart.
    """

    def __init__(self) -> None:
        s = get_settings()
        self.deb = RootDebouncer(quiet_seconds=s.watcher.debounce_seconds)
        self.observer = Observer()
        # root_id -> (watch, abs_path)
        self._watches: dict[int, tuple[object, str]] = {}
        self._lock = threading.Lock()

    def start(self) -> None:
        self.observer.start()
        log.info("watcher observer started")

    def stop(self) -> None:
        try:
            self.observer.stop()
            self.observer.join(timeout=5)
        except Exception:
            log.exception("observer stop failed")

    def reconcile(self) -> None:
        """Sync watch list to DB state. Schedule new roots, unschedule
        ones that were disabled/deleted."""
        try:
            with SessionLocal() as db:
                rows = db.execute(
                    select(Root.id, Root.abs_path, Root.enabled)
                ).all()
        except Exception:
            log.exception("reconcile: failed to fetch roots")
            return

        enabled = {rid: abs_path for (rid, abs_path, en) in rows if en}

        with self._lock:
            current_ids = set(self._watches.keys())
            want_ids = set(enabled.keys())

            # Drop watches for roots that are gone or disabled.
            for rid in current_ids - want_ids:
                watch, ap = self._watches.pop(rid)
                try:
                    self.observer.unschedule(watch)
                    log.info("watcher: unsubscribed root id=%s (%s)", rid, ap)
                except Exception:
                    log.exception("watcher: failed to unschedule root %s", rid)

            # Add new roots, and replace ones whose abs_path changed.
            for rid, ap in enabled.items():
                existing = self._watches.get(rid)
                if existing is not None and existing[1] == ap:
                    continue
                if existing is not None:
                    # path changed → unsubscribe first
                    try:
                        self.observer.unschedule(existing[0])
                    except Exception:
                        pass
                if not os.path.isdir(ap):
                    log.warning("watcher: root %s path missing/not-dir: %s", rid, ap)
                    continue
                try:
                    handler = _RootEventHandler(rid, ap, self.deb)
                    watch = self.observer.schedule(handler, ap, recursive=True)
                    self._watches[rid] = (watch, ap)
                    log.info("watcher: subscribed root id=%s (%s)", rid, ap)
                except OSError as e:
                    # Most common cause: fs.inotify.max_user_watches too low.
                    log.error(
                        "watcher: schedule failed for root %s (%s): %s. "
                        "Consider raising fs.inotify.max_user_watches.",
                        rid, ap, e,
                    )
                except Exception:
                    log.exception("watcher: schedule failed for root %s", rid)


# --- Dispatch loop ----------------------------------------------------------


def _enqueue_scan_if_idle(root_id: int, deb: RootDebouncer) -> bool:
    """Queue a discover_root for `root_id` unless one is already queued
    or running. Marks the root inflight on success."""
    with SessionLocal() as db:
        already = db.execute(
            select(Job.id)
            .where(
                Job.status.in_(("queued", "running")),
                Job.kind == "discover_root",
            )
        ).first()
        # Coarse check: if any discover_root is pending, we'd rather hold
        # off than stack scans. (We can't filter by root_id in payload
        # cheaply without a JSON query; serializing on "any discover" is
        # fine — the worker drains them in order anyway.)
        if already is not None:
            return False
        jobs_mod.enqueue(db, kind="discover_root", payload={"root_id": root_id}, priority=5)
        db.commit()
    deb.mark_inflight(root_id)
    log.info("watcher: enqueued discover_root for root id=%s", root_id)
    return True


def _dispatcher_loop(svc: WatcherService) -> None:
    """Periodically drains the debouncer + clears stale inflight flags."""
    log.info("watcher dispatcher started")
    while not _shutdown.is_set():
        try:
            for rid in svc.deb.drain_due():
                _enqueue_scan_if_idle(rid, svc.deb)
            # Clear inflight flags for roots whose discover_root job
            # finished — cheap poll, lets new events queue another scan.
            with SessionLocal() as db:
                running_or_queued = db.execute(
                    select(Job.payload)
                    .where(
                        Job.status.in_(("queued", "running")),
                        Job.kind == "discover_root",
                    )
                ).all()
            import json
            active_root_ids: set[int] = set()
            for (payload,) in running_or_queued:
                try:
                    active_root_ids.add(int(json.loads(payload).get("root_id")))
                except Exception:
                    pass
            # Any root we marked inflight but no longer has an active
            # discover job → safe to clear.
            for rid in list(svc.deb._inflight):
                if rid not in active_root_ids:
                    svc.deb.clear_inflight(rid)
        except Exception:
            log.exception("dispatcher iteration failed")
        _shutdown.wait(2.0)
    log.info("watcher dispatcher stopped")


def _reconcile_loop(svc: WatcherService) -> None:
    interval = max(15, get_settings().watcher.reconcile_roots_seconds)
    log.info("watcher reconciler started (interval=%ds)", interval)
    while not _shutdown.is_set():
        svc.reconcile()
        _shutdown.wait(interval)
    log.info("watcher reconciler stopped")


# --- Entry point ------------------------------------------------------------


def main() -> int:
    ensure_runtime_dirs()
    _configure_logging()
    _install_signal_handlers()

    s = get_settings()
    if not s.watcher.enabled:
        log.info("watcher disabled in config (watcher.enabled=false). Exiting.")
        return 0

    # Sanity check the DB before we start watching the world.
    try:
        from sqlalchemy import text
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        log.info("db connection ok")
    except Exception:
        log.exception("db connection failed; exiting")
        return 1

    svc = WatcherService()
    svc.reconcile()       # initial subscriptions before observer starts polling
    svc.start()

    # Catch-up: schedule a discover_root for every enabled root so any
    # changes made while the watcher was down get picked up. Reuses the
    # debouncer so it merges with any startup-time events.
    if s.watcher.initial_scan_on_start:
        with svc._lock:
            for rid in svc._watches.keys():
                svc.deb.touch(rid)
        log.info("watcher: catch-up touched %d root(s)", len(svc._watches))

    threads = [
        threading.Thread(target=_dispatcher_loop, args=(svc,), daemon=True, name="dispatch"),
        threading.Thread(target=_reconcile_loop, args=(svc,), daemon=True, name="reconcile"),
    ]
    for t in threads:
        t.start()

    _shutdown.wait()
    svc.stop()
    for t in threads:
        t.join(timeout=5)
    log.info("watcher stopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

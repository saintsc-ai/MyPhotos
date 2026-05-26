"""Per-root debounce/coalescer.

inotify fires one event per file (and sometimes per attribute change),
so copying a 5,000-photo folder can produce >10k events in a few seconds.
Instead of acting on each, we just mark "this root is dirty" and let a
single discover_root job run once the dust settles.

Thread-safe — the watcher's event handler thread calls touch(), the
scheduler thread calls drain_due().
"""

from __future__ import annotations

import threading
import time


class RootDebouncer:
    """Tracks 'last seen event' per root_id; emits roots whose quiet
    period has elapsed.

    Usage:
        deb = RootDebouncer(quiet_seconds=30)
        deb.touch(root_id)                       # from event handler
        for rid in deb.drain_due(now):           # from scheduler loop
            enqueue_scan(rid)
    """

    def __init__(self, quiet_seconds: int) -> None:
        self._quiet = quiet_seconds
        self._lock = threading.Lock()
        # root_id -> monotonic timestamp of last event
        self._last: dict[int, float] = {}
        # root_id -> True while a scan is currently inflight (queued or
        # running). Suppresses re-queueing so a long-running discover_root
        # doesn't get stacked behind itself.
        self._inflight: set[int] = set()

    def touch(self, root_id: int) -> None:
        with self._lock:
            self._last[root_id] = time.monotonic()

    def mark_inflight(self, root_id: int) -> None:
        with self._lock:
            self._inflight.add(root_id)

    def clear_inflight(self, root_id: int) -> None:
        with self._lock:
            self._inflight.discard(root_id)

    def drain_due(self, now: float | None = None) -> list[int]:
        """Return root_ids whose last event is older than quiet_seconds.
        Those roots are removed from the queue (caller is expected to
        enqueue a scan and mark_inflight)."""
        if now is None:
            now = time.monotonic()
        due: list[int] = []
        with self._lock:
            for rid, ts in list(self._last.items()):
                if rid in self._inflight:
                    continue
                if now - ts >= self._quiet:
                    due.append(rid)
                    del self._last[rid]
        return due

    def pending(self) -> int:
        with self._lock:
            return len(self._last)

"""Watcher must exit cleanly (0) when the tree exceeds the inotify watch
limit — a non-zero exit under Restart=on-failure turned into a restart
loop that re-walked the whole library every minute."""

from __future__ import annotations

import errno
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine

from app.watcher import main as watcher_main


class _FakeService:
    def __init__(self, exc: OSError):
        self._exc = exc
        self.stopped = False
        self.deb = SimpleNamespace(pending=lambda: 0)

    def reconcile(self) -> None:
        pass

    def start(self) -> None:
        raise self._exc

    def stop(self) -> None:
        self.stopped = True


def _patch(monkeypatch, exc: OSError) -> tuple[_FakeService, list[str]]:
    svc = _FakeService(exc)
    notes: list[str] = []
    settings = SimpleNamespace(watcher=SimpleNamespace(enabled=True))
    monkeypatch.setattr(watcher_main, "ensure_runtime_dirs", lambda: None)
    monkeypatch.setattr(watcher_main, "_configure_logging", lambda: None)
    monkeypatch.setattr(watcher_main, "_install_signal_handlers", lambda: None)
    monkeypatch.setattr(watcher_main, "get_settings", lambda: settings)
    monkeypatch.setattr(watcher_main, "engine", create_engine("sqlite:///:memory:"))
    monkeypatch.setattr(watcher_main, "WatcherService", lambda: svc)
    monkeypatch.setattr(watcher_main, "_write_heartbeat",
                        lambda _svc, note="": notes.append(note))
    return svc, notes


def test_watch_limit_exits_zero(monkeypatch):
    svc, notes = _patch(monkeypatch, OSError(errno.ENOSPC, "inotify watch limit reached"))
    assert watcher_main.main() == 0
    assert svc.stopped
    assert notes and "inotify limit" in notes[0]


def test_other_oserror_still_raises(monkeypatch):
    _patch(monkeypatch, OSError(errno.EACCES, "permission denied"))
    with pytest.raises(OSError):
        watcher_main.main()

"""Worker entry point.

MVP 1: just an idle loop that proves the process starts, configures logging,
opens the DB, and shuts down cleanly on SIGINT/SIGTERM. The scanner, job
dispatcher, EXIF and thumbnail stages land in MVP 2.

Run with: python -m app.worker.main
"""

from __future__ import annotations

import logging
import signal
import sys
import threading
import time

from sqlalchemy import text

from ..config import get_settings
from ..db import engine
from ..paths import LOGS_DIR, ensure_runtime_dirs


def _configure_logging() -> None:
    settings = get_settings()
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=settings.logging.level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[
            logging.StreamHandler(sys.stdout),
        ],
    )


_shutdown = threading.Event()


def _install_signal_handlers() -> None:
    def _handler(signum, _frame):
        logging.getLogger(__name__).info("received signal %s, shutting down", signum)
        _shutdown.set()

    signal.signal(signal.SIGINT, _handler)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, _handler)


def _ping_db() -> None:
    with engine.connect() as conn:
        conn.execute(text("SELECT 1"))


def main() -> int:
    ensure_runtime_dirs()
    _configure_logging()
    _install_signal_handlers()

    log = logging.getLogger("worker")
    settings = get_settings()
    log.info("worker starting (concurrency=%d)", settings.worker.concurrency)

    try:
        _ping_db()
        log.info("db connection ok")
    except Exception:
        log.exception("db connection failed; exiting")
        return 1

    # MVP 1: idle loop. Replace with job dispatcher + scanner in MVP 2.
    while not _shutdown.is_set():
        _shutdown.wait(settings.worker.idle_poll_seconds)

    log.info("worker stopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

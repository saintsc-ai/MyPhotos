"""ML worker entry point.

Run with: python -m app.worker_ml.main
"""

from __future__ import annotations

import logging
import signal
import sys
import threading

from sqlalchemy import text

from ..config import get_settings
from ..db import engine
from ..paths import LOGS_DIR, ensure_runtime_dirs
from . import dispatcher

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
    def _handler(signum, _frame):
        logging.getLogger(__name__).info("ml worker: signal %s, stopping", signum)
        _shutdown.set()

    signal.signal(signal.SIGINT, _handler)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, _handler)


def main() -> int:
    ensure_runtime_dirs()
    _configure_logging()
    _install_signal_handlers()
    log = logging.getLogger("ml-worker")

    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        log.info("ml worker: db connection ok")
    except Exception:
        log.exception("ml worker: db connection failed; exiting")
        return 1

    # Eagerly probe the YOLO model — failure here just means the install
    # script wasn't run yet; loop will still poll for jobs and skip cleanly.
    from .yolo import MODEL_PATH

    if MODEL_PATH.exists():
        log.info("yolo model found: %s", MODEL_PATH)
    else:
        log.warning(
            "yolo model missing at %s — run scripts/install-ml-models.sh",
            MODEL_PATH,
        )

    dispatcher.run(_shutdown)
    log.info("ml worker stopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

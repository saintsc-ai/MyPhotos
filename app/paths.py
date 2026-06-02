"""Filesystem paths used across the app.

All paths are derived from PROJECT_ROOT so the whole folder can be moved
to another host without touching code. Environment variables can override
DATA_DIR / CONFIG_DIR for setups where catalog/cache live on a different volume.
"""

from __future__ import annotations

import os
from pathlib import Path

PROJECT_ROOT: Path = Path(__file__).resolve().parent.parent

DATA_DIR: Path = Path(os.environ.get("MYPHOTOS_DATA", PROJECT_ROOT / "data"))
CONFIG_DIR: Path = Path(os.environ.get("MYPHOTOS_CONFIG", PROJECT_ROOT / "config"))
VENDOR_DIR: Path = PROJECT_ROOT / "vendor"

DB_PATH: Path = DATA_DIR / "catalog.db"
THUMBS_DIR: Path = DATA_DIR / "thumbs"
# Lazily-built H.264 web-playable video proxies, keyed by source sha256.
PROXIES_DIR: Path = DATA_DIR / "proxies"
LOGS_DIR: Path = DATA_DIR / "logs"
STATE_DIR: Path = DATA_DIR / "state"
TRASH_DIR: Path = DATA_DIR / "trash"
# Short-lived scratch (bulk-download zips, etc.). Files here are
# garbage-collected best-effort; safe to wipe at any time.
TMP_DIR: Path = DATA_DIR / "tmp"


def ensure_runtime_dirs() -> None:
    """Create all runtime directories. Safe to call repeatedly."""
    for d in (DATA_DIR, THUMBS_DIR, PROXIES_DIR, LOGS_DIR, STATE_DIR, TRASH_DIR, TMP_DIR):
        d.mkdir(parents=True, exist_ok=True)

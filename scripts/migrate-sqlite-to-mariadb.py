#!/usr/bin/env python3
"""Compatibility wrapper around scripts/migrate-db.py.

Kept so the original README snippet keeps working. New invocations
should use migrate-db.py directly — it accepts arbitrary src/dst
URLs (MariaDB → SQLite, SQLite → SQLite, etc.) rather than assuming
the SQLite → MariaDB direction.

Usage (unchanged):
    scripts/migrate-sqlite-to-mariadb.py \\
        "mysql+pymysql://user:pass@host:3306/myphotos?charset=utf8mb4" \\
        [--sqlite data/catalog.db] \\
        [--batch 1000] \\
        [--drop]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("dest_url", help="MariaDB/MySQL DSN")
    ap.add_argument("--sqlite", default="data/catalog.db", help="Source SQLite file")
    ap.add_argument("--batch", type=int, default=1000)
    ap.add_argument("--drop", action="store_true")
    args = ap.parse_args()

    src_path = Path(args.sqlite).resolve()
    src_url = f"sqlite:///{src_path.as_posix()}"

    # Hand off to the generalized script — preserves arg semantics.
    here = Path(__file__).resolve().parent
    migrate = here / "migrate-db.py"
    cmd = [sys.executable, str(migrate), src_url, args.dest_url, "--batch", str(args.batch)]
    if args.drop:
        cmd.append("--drop")
    import os
    os.execv(sys.executable, cmd)


if __name__ == "__main__":
    raise SystemExit(main())

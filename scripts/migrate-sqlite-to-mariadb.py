#!/usr/bin/env python3
"""Copy a MyPhotos SQLite catalog into a fresh MariaDB schema.

Usage:
    scripts/migrate-sqlite-to-mariadb.py \\
        "mysql+pymysql://user:pass@host:3306/myphotos?charset=utf8mb4" \\
        [--sqlite data/catalog.db] \\
        [--batch 1000] \\
        [--drop]

What it does:
    1. Bind to the source SQLite file and the target MariaDB DSN.
    2. Create the schema on the target via SQLAlchemy metadata
       (no alembic — equivalent to `alembic upgrade head` with the
       current model definitions).
    3. Copy every row in topo order (parents before children) in
       batches. Composite-PK rows are written with bulk insert so the
       row order from the source is preserved.
    4. Verify row counts match.

Run while the app is **stopped** so no new rows arrive during the copy.

Safety:
    - --drop will DROP ALL TABLES on the target before recreating them.
      Without it, the script aborts if any target table already has rows.
    - The script never touches the source SQLite file (read-only).
    - Auto-increment sequences are reset after copy so subsequent
      INSERTs do not collide with existing IDs.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Add project root to PYTHONPATH so 'app.*' imports work when run from anywhere.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import create_engine, func, select, text
from sqlalchemy.orm import Session

from app.models import Base


def _row_count(session: Session, table) -> int:
    return session.execute(select(func.count()).select_from(table)).scalar_one()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("dest_url", help="MariaDB/MySQL DSN (mysql+pymysql://...)")
    ap.add_argument(
        "--sqlite",
        default="data/catalog.db",
        help="Source SQLite file (default: data/catalog.db)",
    )
    ap.add_argument(
        "--batch", type=int, default=1000, help="Insert batch size (default: 1000)"
    )
    ap.add_argument(
        "--drop",
        action="store_true",
        help="DROP ALL TABLES on target first (destructive)",
    )
    args = ap.parse_args()

    src_path = Path(args.sqlite).resolve()
    if not src_path.exists():
        print(f"ERROR: source SQLite file not found: {src_path}", file=sys.stderr)
        return 1

    src_url = f"sqlite:///{src_path.as_posix()}"
    print(f"==> source: {src_url}")
    print(f"==> target: {args.dest_url}")

    src_eng = create_engine(src_url, future=True)
    dst_eng = create_engine(args.dest_url, future=True)

    # Quick connectivity check before doing anything destructive.
    with dst_eng.connect() as conn:
        conn.execute(text("SELECT 1"))
    print("==> target reachable")

    # --- (Re)create schema on target ---
    if args.drop:
        print("==> dropping existing tables on target")
        Base.metadata.drop_all(dst_eng)
    print("==> creating schema on target")
    Base.metadata.create_all(dst_eng)

    # Refuse to overwrite a populated target unless --drop was passed.
    if not args.drop:
        with Session(dst_eng) as s:
            for table in Base.metadata.sorted_tables:
                n = _row_count(s, table)
                if n > 0:
                    print(
                        f"ERROR: target {table.name} already has {n} rows. "
                        "Use --drop to wipe and recopy.",
                        file=sys.stderr,
                    )
                    return 2

    # --- Copy rows in parent → child order ---
    tables = list(Base.metadata.sorted_tables)
    src_counts: dict[str, int] = {}
    with Session(src_eng) as src:
        for t in tables:
            src_counts[t.name] = _row_count(src, t)
        print("==> source counts:", ", ".join(
            f"{n}:{src_counts[n.name]}" for n in tables if src_counts[n.name]
        ) or "(empty)")

        for t in tables:
            total = src_counts[t.name]
            if total == 0:
                continue
            print(f"==> copying {t.name} ({total} rows)")
            with dst_eng.begin() as dst:
                offset = 0
                while offset < total:
                    rows = src.execute(
                        t.select().limit(args.batch).offset(offset)
                    ).all()
                    if not rows:
                        break
                    payload = [dict(r._mapping) for r in rows]
                    dst.execute(t.insert(), payload)
                    offset += len(rows)
                    print(f"    {offset}/{total}")

    # --- Reset auto-increment sequences ---
    print("==> resetting auto-increment counters")
    with dst_eng.begin() as dst:
        # MariaDB / MySQL only — use ALTER TABLE ... AUTO_INCREMENT.
        for t in tables:
            pk_cols = [c for c in t.primary_key.columns]
            if len(pk_cols) != 1:
                continue
            pk = pk_cols[0]
            if not pk.autoincrement:
                continue
            try:
                max_id = dst.execute(
                    select(func.coalesce(func.max(pk), 0))
                ).scalar_one()
                next_id = int(max_id) + 1
                dst.execute(text(f"ALTER TABLE {t.name} AUTO_INCREMENT = {next_id}"))
            except Exception as e:
                print(f"    skip {t.name}: {e}")

    # --- Verify counts ---
    print("==> verifying row counts")
    ok = True
    with Session(dst_eng) as dst:
        for t in tables:
            sn = src_counts[t.name]
            dn = _row_count(dst, t)
            mark = "OK " if sn == dn else "!! "
            if sn != dn:
                ok = False
            if sn or dn:
                print(f"    {mark}{t.name}: src={sn} dst={dn}")
    if not ok:
        print("ERROR: row counts diverge — review output above.", file=sys.stderr)
        return 3

    print("==> done. To switch the app over, set in config/local.toml:")
    print(f"    [database]\n    url = \"{args.dest_url}\"")
    print("    then restart the API + worker services.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

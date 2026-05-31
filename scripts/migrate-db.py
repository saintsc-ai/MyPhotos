#!/usr/bin/env python3
"""Copy the MyPhotos catalog from one database to another.

Works in any direction supported by SQLAlchemy + our model definitions:
SQLite ↔ MariaDB ↔ PostgreSQL, plus same-dialect copies.

Usage:
    scripts/migrate-db.py SRC_URL DST_URL [--batch 1000] [--drop]

URLs are SQLAlchemy DSNs:
    sqlite:///absolute/or/relative/path.db
    mysql+pymysql://user:pass@host:3306/dbname?charset=utf8mb4
    postgresql+psycopg://user:pass@host:5432/dbname

Examples:
    # SQLite → MariaDB (typical promotion)
    scripts/migrate-db.py \\
        sqlite:///data/catalog.db \\
        "mysql+pymysql://myphotos:pw@127.0.0.1:3306/myphotos?charset=utf8mb4" \\
        --drop

    # SQLite → PostgreSQL
    scripts/migrate-db.py \\
        sqlite:///data/catalog.db \\
        "postgresql+psycopg://myphotos:pw@127.0.0.1:5432/myphotos" \\
        --drop

    # MariaDB → SQLite (reverting to single-file backend)
    scripts/migrate-db.py \\
        "mysql+pymysql://myphotos:pw@127.0.0.1:3306/myphotos?charset=utf8mb4" \\
        sqlite:///data/catalog.db \\
        --drop

    # SQLite → SQLite (compact / clone a catalog)
    scripts/migrate-db.py \\
        sqlite:///data/catalog.db \\
        sqlite:///data/catalog.new.db \\
        --drop

What it does:
    1. Connects to both ends, verifies reachability.
    2. (Optional --drop) drops every Base.metadata table on the target.
    3. Re-creates the schema on the target via SQLAlchemy metadata
       (no alembic), matching the current model definitions.
    4. Refuses to overwrite an already-populated target unless --drop.
    5. Copies every row in parent → child order, in batches.
    6. Resets auto-increment counters past the imported max id:
         - MariaDB / MySQL: ALTER TABLE ... AUTO_INCREMENT = N
         - PostgreSQL:      setval() on the column's owned sequence
         - SQLite:          implicit (next rowid = MAX(rowid)+1)
    7. Verifies row counts match on both sides.

Run while the app is STOPPED so no new rows arrive mid-copy.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Allow `from app.* import` from any cwd.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import create_engine, func, select, text
from sqlalchemy.orm import Session

from app.models import Base


def _row_count(session: Session, table) -> int:
    return session.execute(select(func.count()).select_from(table)).scalar_one()


def _is_sqlite(url: str) -> bool:
    return url.startswith("sqlite")


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("src_url", help="Source DB URL (SQLAlchemy DSN)")
    ap.add_argument("dst_url", help="Destination DB URL (SQLAlchemy DSN)")
    ap.add_argument(
        "--batch", type=int, default=1000, help="Insert batch size (default: 1000)"
    )
    ap.add_argument(
        "--drop",
        action="store_true",
        help="DROP ALL TABLES on target first (destructive)",
    )
    args = ap.parse_args()

    if args.src_url == args.dst_url:
        print("ERROR: src and dst URLs are identical", file=sys.stderr)
        return 1

    print(f"==> source: {args.src_url}")
    print(f"==> target: {args.dst_url}")

    src_eng = create_engine(args.src_url, future=True)
    dst_eng = create_engine(args.dst_url, future=True)

    # Connectivity check.
    for label, eng in (("source", src_eng), ("target", dst_eng)):
        try:
            with eng.connect() as conn:
                conn.execute(text("SELECT 1"))
        except Exception as e:
            print(f"ERROR: cannot connect to {label}: {e}", file=sys.stderr)
            return 1

    # --- (Re)create schema on target ---
    if args.drop:
        print("==> dropping existing tables on target")
        Base.metadata.drop_all(dst_eng)
    print("==> creating schema on target")
    Base.metadata.create_all(dst_eng)

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

    tables = list(Base.metadata.sorted_tables)
    src_counts: dict[str, int] = {}
    with Session(src_eng) as src:
        for t in tables:
            src_counts[t.name] = _row_count(src, t)
        nonempty = [n for n in tables if src_counts[n.name]]
        if not nonempty:
            print("==> source is empty — only schema was created on target")
            return 0
        print(
            "==> source counts:",
            ", ".join(f"{t.name}:{src_counts[t.name]}" for t in nonempty),
        )

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

    # --- Reset auto-increment sequences on target ---
    # SQLite tracks the next rowid implicitly from MAX(rowid) — no manual
    # reset needed. (sqlite_sequence is only used when AUTOINCREMENT was
    # declared in CREATE TABLE; SQLAlchemy doesn't emit that for us.)
    # MariaDB / MySQL use a per-table AUTO_INCREMENT counter.
    # PostgreSQL uses sequences attached to columns — and crucially the
    # sequence is only advanced by nextval(), so explicit-id INSERTs
    # (which is what we do here) leave the sequence stuck at 1.
    dst_dialect = dst_eng.dialect.name
    if dst_dialect == "sqlite":
        pass
    elif dst_dialect in ("mysql", "mariadb"):
        print("==> resetting target AUTO_INCREMENT counters (mysql/mariadb)")
        with dst_eng.begin() as dst:
            for t in tables:
                pk_cols = list(t.primary_key.columns)
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
                    dst.execute(
                        text(f"ALTER TABLE {t.name} AUTO_INCREMENT = {next_id}")
                    )
                except Exception as e:
                    print(f"    skip {t.name}: {e}")
    elif dst_dialect == "postgresql":
        print("==> resetting target sequences (postgresql)")
        with dst_eng.begin() as dst:
            for t in tables:
                pk_cols = list(t.primary_key.columns)
                if len(pk_cols) != 1:
                    continue
                pk = pk_cols[0]
                if not pk.autoincrement:
                    continue
                try:
                    # Find the sequence backing this column. SERIAL /
                    # IDENTITY columns get one auto-created and owned
                    # by the column; pg_get_serial_sequence returns
                    # NULL for non-serial columns (e.g. plain INTEGER
                    # PK).
                    seq = dst.execute(text(
                        "SELECT pg_get_serial_sequence(:t, :c)"
                    ), {"t": t.name, "c": pk.name}).scalar()
                    if not seq:
                        continue
                    # setval(seq, max_id, true) leaves is_called=true
                    # so the next nextval() returns max_id+1.
                    # GREATEST(1, ...) handles empty tables (sequences
                    # must be advanced to >= 1).
                    dst.execute(text(
                        f"SELECT setval(:seq, "
                        f"GREATEST(1, COALESCE((SELECT MAX({pk.name}) FROM {t.name}), 1)), "
                        f"true)"
                    ), {"seq": seq})
                except Exception as e:
                    print(f"    skip {t.name}: {e}")
    else:
        print(f"WARN: no sequence-reset strategy for dialect {dst_dialect!r} — skipping")

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

    print("==> done. To use the new backend, set in config/local.toml:")
    if _is_sqlite(args.dst_url):
        print('    [database]\n    url = ""    # SQLite default at data/catalog.db')
    else:
        print(f'    [database]\n    url = "{args.dst_url}"')
    print("    then restart the API + worker services.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

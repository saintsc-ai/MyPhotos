"""Database engine and session factory.

Two supported backends:

- **SQLite** (default, recommended). Single file under data/catalog.db.
  WAL mode keeps reads non-blocking while the worker writes; PRAGMA
  busy_timeout / cache_size are tuned for the indexing churn.
- **MariaDB / MySQL** (opt-in). Set `database.url` in config to a
  DSN like `mysql+pymysql://user:pass@host:3306/myphotos?charset=utf8mb4`.
  Connection pool uses pre-ping + recycle so long-idle workers survive
  the server's `wait_timeout`.

Dialect selection happens once at import time. Use the same DSN in
alembic/env.py via `app.config.get_settings()` so migrations target the
chosen backend.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from .config import get_settings
from .paths import DB_PATH, ensure_runtime_dirs


def resolve_db_url() -> str:
    """Return the SQLAlchemy URL for the configured backend.

    Empty `database.url` → bundled SQLite file. Exposed for alembic/env.py
    and for the migration helper script.
    """
    url = (get_settings().database.url or "").strip()
    if url:
        return url
    ensure_runtime_dirs()
    return f"sqlite:///{DB_PATH.as_posix()}"


def is_sqlite_url(url: str) -> bool:
    return url.startswith("sqlite")


_DB_URL = resolve_db_url()
_IS_SQLITE = is_sqlite_url(_DB_URL)


def _build_engine(url: str, is_sqlite: bool) -> Engine:
    if is_sqlite:
        return create_engine(
            url,
            future=True,
            # check_same_thread=False so the API can hand sessions across threads
            connect_args={"check_same_thread": False, "timeout": 30},
        )
    # MariaDB / MySQL — pool tuned for long-lived worker connections.
    return create_engine(
        url,
        future=True,
        # Survive MariaDB `wait_timeout` (default 8h) and any NAT idle drops
        # without throwing on the first reused connection of the day.
        pool_pre_ping=True,
        pool_recycle=3600,
        # Modest pool — most write traffic is single-threaded inside one
        # worker. Adjust if you raise worker.concurrency dramatically.
        pool_size=10,
        max_overflow=10,
    )


engine: Engine = _build_engine(_DB_URL, _IS_SQLITE)


if _IS_SQLITE:
    @event.listens_for(engine, "connect")
    def _set_sqlite_pragmas(dbapi_connection, _connection_record) -> None:
        cur = dbapi_connection.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA synchronous=NORMAL")
        cur.execute("PRAGMA foreign_keys=ON")
        # Workers contend on writes during indexing — give the lock more time
        # before bailing out so retries are unnecessary. Bulk discover_root
        # batches can hold the writer lock for >15s on large roots, and the
        # ML worker (classify_embedding, etc.) writes alongside the indexing
        # worker, so 60s gives some headroom before SQLite gives up.
        cur.execute("PRAGMA busy_timeout=60000")
        cur.execute("PRAGMA temp_store=MEMORY")
        # 64 MB page cache (negative = KB). Keeps hot pages (jobs, photos
        # indexes) in memory across workers; significant for the
        # claim_one / status-update churn.
        cur.execute("PRAGMA cache_size=-65536")
        # mmap_size lets SQLite memory-map the DB file (up to this many
        # bytes) so reads bypass the OS page-cache copy. 256 MB covers
        # the entire catalog for personal libraries and a working set
        # for bigger ones — speeds up index scans noticeably on the
        # gallery list / map cluster queries. No effect on writes.
        cur.execute("PRAGMA mmap_size=268435456")
        cur.close()


# MariaDB / MySQL NULLS LAST workaround.
#
# SQLAlchemy's `.nullslast()` / `.nullsfirst()` compile to the standard
# "NULLS LAST" / "NULLS FIRST" SQL — accepted by SQLite and PostgreSQL.
# MariaDB (including 12.x) rejects this in ORDER BY with
#   ERROR 1064 syntax error near 'NULLS LAST'
# (MariaDB added it for window functions only in 10.6, not for plain
# ORDER BY clauses.) The gallery query
#   ORDER BY photos.taken_at DESC NULLS LAST, ...
# therefore 500'd as soon as a user switched the catalog to MariaDB.
#
# Register a dialect-scoped compiler that rewrites these two modifiers
# into the (col IS NULL, col DIR) idiom which works on every dialect.
# This way none of the application-level call sites
# (Photo.taken_at.desc().nullslast() etc.) need to change.
from sqlalchemy.ext.compiler import compiles  # noqa: E402
from sqlalchemy.sql import operators as _sa_ops  # noqa: E402
from sqlalchemy.sql.elements import UnaryExpression  # noqa: E402


@compiles(UnaryExpression, "mysql")
def _mysql_compile_nulls_modifier(element, compiler, **kw):
    mod = getattr(element, "modifier", None)
    if mod in (_sa_ops.nulls_last_op, _sa_ops.nullslast_op) or mod in (
        _sa_ops.nulls_first_op, _sa_ops.nullsfirst_op
    ):
        # element.element is the wrapped expression. For the typical
        # call .desc().nullslast() it's another UnaryExpression
        # (desc/asc); unwrap once to find the bare column so the
        # NULL-presence prefix references THAT, not "col DESC".
        inner = element.element
        if isinstance(inner, UnaryExpression):
            bare = inner.element
            direction = inner            # preserve .desc() / .asc()
        else:
            bare = inner
            direction = inner            # bare column → no direction wrapper
        # NULLs LAST  → (col IS NULL) ASC first: 0 (NOT NULL) before 1 (NULL)
        # NULLs FIRST → (col IS NOT NULL) ASC: 0 (NULL) before 1 (NOT NULL)
        if mod in (_sa_ops.nulls_last_op, _sa_ops.nullslast_op):
            prefix = bare.is_(None)
        else:
            prefix = bare.isnot(None)
        return "%s, %s" % (
            compiler.process(prefix, **kw),
            compiler.process(direction, **kw),
        )
    # Every other UnaryExpression (NOT, DISTINCT, desc/asc on their
    # own, +/-) falls through to SQLAlchemy's built-in compiler.
    return compiler.visit_unary(element, **kw)


SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)


@contextmanager
def session_scope() -> Iterator[Session]:
    """Transactional session for short-lived units of work."""
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()

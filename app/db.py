"""SQLite engine and session factory.

WAL mode is essential: API and worker access the same DB file concurrently.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from .paths import DB_PATH, ensure_runtime_dirs


def _sqlite_url() -> str:
    ensure_runtime_dirs()
    # 4 slashes for absolute path on both Windows and POSIX
    return f"sqlite:///{DB_PATH.as_posix()}"


engine: Engine = create_engine(
    _sqlite_url(),
    future=True,
    # check_same_thread=False so the API can hand sessions across threads
    connect_args={"check_same_thread": False, "timeout": 30},
)


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
    cur.close()


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

"""Shared pytest fixtures for the test suite.

Design choices:

- **In-memory SQLite per test**. Each test gets a fresh engine + all
  tables via `Base.metadata.create_all`. We deliberately bypass alembic
  so the suite isn't sensitive to revision drift while still exercising
  the real schema.
- **Direct DB session, not the FastAPI app**. The endpoints pull in a
  lot of bootstrap (session middleware, audit, etc.); for the first
  round of tests it's higher signal-to-noise to call the helper
  functions (auth_acl, fts, trash machinery) against the session
  directly.
- **One root + a few photos by default**. Most tests need at least one
  Root and a small number of Photos to evaluate ACL filters; factories
  keep that overhead out of each individual test.

To run:
    cd MyPhotos && uv run pytest         # or: python -m pytest
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.models import Base, FolderACL, Photo, Root, RootACL, User


@pytest.fixture(autouse=True)
def _reset_fts_cache():
    """The FTS module memoises `is_available` in a process-global so
    production doesn't re-probe sqlite_master on every request. In
    tests that means a previous test's verdict leaks into the next —
    flush before each test so the cache reflects the per-test DB."""
    from app import fts as _fts
    _fts._reset_availability_cache()
    yield
    _fts._reset_availability_cache()


@pytest.fixture
def engine():
    """Per-test in-memory SQLite engine with the full schema applied.

    `check_same_thread=False` is needed because pytest sometimes spawns
    finalizers on another thread. We're single-writer either way.
    """
    eng = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        future=True,
    )
    Base.metadata.create_all(eng)
    yield eng
    eng.dispose()


@pytest.fixture
def db(engine) -> Iterator[Session]:
    """Open a Session against the per-test engine and clean up after."""
    with Session(engine) as session:
        yield session


@pytest.fixture
def fts_db(engine, db: Session) -> Session:
    """Like `db` but also creates the FTS5 photo_fts virtual table the
    same way alembic 0020 does. Use this in tests that exercise
    `app.fts` so `is_available` flips True without running the real
    migration. The module-level availability cache is reset so the
    detection is fresh per test.
    """
    db.connection().exec_driver_sql(
        "CREATE VIRTUAL TABLE photo_fts USING fts5(text, tokenize='trigram')"
    )
    db.commit()
    # Flush the module-global feature-detect cache so we don't carry a
    # `False` from an earlier test that ran without the FTS table.
    from app import fts as _fts
    _fts._reset_availability_cache()
    return db


# ----- factories -----


def make_user(
    db: Session,
    *,
    username: str = "u",
    display_name: str | None = None,
    is_admin: bool = False,
    can_delete: bool = True,
    can_share: bool = True,
    can_upload: bool = True,
    can_edit_meta_others: bool = True,
) -> User:
    """Insert + return a User. Defaults grant all per-user permission
    flags so tests opt out of capabilities rather than opting in — most
    ACL behaviour is independent of these flags. Password hash is a
    sentinel; nothing under test verifies real passwords. display_name
    defaults to the username when not given."""
    u = User(
        username=username,
        display_name=display_name or username,
        password_hash="x" * 60,
        is_admin=is_admin,
        can_upload=can_upload,
        can_delete=can_delete,
        can_share=can_share,
        can_edit_meta_others=can_edit_meta_others,
    )
    db.add(u)
    db.flush()
    return u


def make_root(db: Session, *, label: str = "default", abs_path: str = "/tmp/r") -> Root:
    r = Root(label=label, abs_path=abs_path, readonly=False, enabled=True)
    db.add(r)
    db.flush()
    return r


def make_photo(
    db: Session,
    root: Root,
    *,
    rel_path: str = "a.jpg",
    filename: str | None = None,
    media_kind: str = "image",
    ext: str = "jpg",
    owner_user_id: int | None = None,
    visibility: str = "inherit",
    status: str = "active",
    description: str | None = None,
) -> Photo:
    p = Photo(
        root_id=root.id,
        rel_path=rel_path,
        filename=filename or rel_path.rsplit("/", 1)[-1],
        ext=ext,
        media_kind=media_kind,
        owner_user_id=owner_user_id,
        visibility=visibility,
        status=status,
        description=description,
    )
    db.add(p)
    db.flush()
    return p


def grant_root(db: Session, root: Root, user: User, level: str) -> None:
    db.add(RootACL(root_id=root.id, user_id=user.id, level=level))
    db.flush()


def grant_folder(
    db: Session, root: Root, user: User, prefix: str, level: str
) -> None:
    """`prefix` should already include the trailing slash that the SQL
    machinery expects (e.g. 'family/private/')."""
    db.add(FolderACL(
        root_id=root.id, user_id=user.id, path_prefix=prefix, level=level,
    ))
    db.flush()

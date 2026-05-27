"""Tests for the trash machinery — move, restore, permanent purge.

These tests exercise the pure helpers (`_move_to_trash`,
`_restore_one`, `_purge_one`) rather than the HTTP endpoints — the
endpoints are thin wrappers around them, and the file-system round
trip is the part most likely to regress.

Key fixture trick: `TRASH_DIR` is bound into each module at import
time (`from ..paths import TRASH_DIR`), so we monkeypatch each
binding individually. Touching `app.paths.TRASH_DIR` alone would
*not* redirect the already-bound names in `routes_photos` and
`routes_trash`.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from app.admin.routes_trash import _purge_one, _restore_one
from app.api.routes_photos import _move_to_trash
from app.models import Photo, Root
from tests.conftest import make_photo, make_root, make_user


@pytest.fixture
def trash_env(tmp_path, monkeypatch):
    """Rewire every TRASH_DIR import (and the source-root path used by
    move/restore) to live inside the per-test tmp_path. Returns
    `(roots_dir, trash_dir, factory)` where factory(rel_path, data)
    creates a file at roots/<rel_path>.
    """
    roots_dir = tmp_path / "roots" / "main"
    trash_dir = tmp_path / "data" / "trash"
    roots_dir.mkdir(parents=True)
    trash_dir.mkdir(parents=True)
    monkeypatch.setattr("app.admin.routes_trash.TRASH_DIR", trash_dir)
    monkeypatch.setattr("app.api.routes_photos.TRASH_DIR", trash_dir)
    # `_check_trash_space` consults shutil.disk_usage(TRASH_DIR); leave
    # the real impl in place — tmp_path is on the same volume as the
    # repo so disk_usage works fine.

    def write_file(rel_path: str, data: bytes = b"fake jpeg bytes") -> Path:
        full = roots_dir / rel_path
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_bytes(data)
        return full

    yield roots_dir, trash_dir, write_file


def _trash_root(db, roots_dir: Path) -> Root:
    """Make a Root whose abs_path points at the test's roots dir."""
    return make_root(db, label="t", abs_path=str(roots_dir))


# ----- move-to-trash -----


def test_move_to_trash_relocates_file_and_writes_meta(db, trash_env):
    roots_dir, trash_dir, write_file = trash_env
    user = make_user(db, username="del")
    root = _trash_root(db, roots_dir)
    src = write_file("2024/a.jpg", b"hello")
    p = make_photo(
        db, root, rel_path="2024/a.jpg", filename="a.jpg",
        owner_user_id=user.id,
    )
    p.file_size = src.stat().st_size
    db.commit()

    result = _move_to_trash(p, root, user)
    assert result["moved"] is True

    # Source gone, file landed in the trash dir for this photo.
    assert not src.exists()
    landed = trash_dir / str(p.id) / "a.jpg"
    assert landed.exists()
    assert landed.read_bytes() == b"hello"

    # Sidecar carries enough to restore (root id + rel_path + filename).
    meta = json.loads((trash_dir / str(p.id) / "_meta.json").read_text(encoding="utf-8"))
    assert meta["photo_id"] == p.id
    assert meta["original_root_id"] == root.id
    assert meta["original_rel_path"] == "2024/a.jpg"
    assert meta["deleted_by"] == "del"


def test_move_to_trash_missing_source_returns_reason(db, trash_env):
    roots_dir, _trash_dir, _ = trash_env
    user = make_user(db, username="u")
    root = _trash_root(db, roots_dir)
    # No file written — only the DB row exists.
    p = make_photo(db, root, rel_path="ghost.jpg", filename="ghost.jpg")
    db.commit()
    result = _move_to_trash(p, root, user)
    assert result["moved"] is False
    assert "missing" in result["reason"].lower()


def test_move_to_trash_collision_appends_timestamp(db, trash_env):
    """Re-deleting the same id (orphan row replayed) must not clobber
    the prior trashed copy — the second move lands at <ts>_<name>."""
    roots_dir, trash_dir, write_file = trash_env
    user = make_user(db, username="u")
    root = _trash_root(db, roots_dir)
    # Seed a prior file already inside the trash dir.
    prior_dir = trash_dir / "1"
    prior_dir.mkdir()
    (prior_dir / "a.jpg").write_bytes(b"prior")
    # Now stage a fresh source + DB row with the matching id.
    src = write_file("a.jpg", b"fresh")
    p = make_photo(db, root, rel_path="a.jpg", filename="a.jpg")
    # Force the id so the dest dir collides with the seeded one above.
    p.id = 1
    db.commit()

    result = _move_to_trash(p, root, user)
    assert result["moved"] is True
    assert not src.exists()
    # Both copies survive.
    surviving = sorted(x.name for x in prior_dir.iterdir() if x.name != "_meta.json")
    assert "a.jpg" in surviving
    assert any(name.endswith("_a.jpg") and name != "a.jpg" for name in surviving)


# ----- restore -----


def test_restore_round_trip_moves_file_back(db, trash_env):
    roots_dir, trash_dir, write_file = trash_env
    user = make_user(db, username="u")
    root = _trash_root(db, roots_dir)
    src = write_file("vacation/x.jpg", b"original")
    p = make_photo(
        db, root, rel_path="vacation/x.jpg", filename="x.jpg",
        owner_user_id=user.id,
    )
    db.commit()

    moved = _move_to_trash(p, root, user)
    assert moved["moved"] is True
    # The endpoint flips status; mimic that so _restore_one's
    # bookkeeping reflects the real flow.
    p.status = "trashed"
    p.trashed_by_user_id = user.id
    db.commit()
    assert not src.exists()

    outcome = _restore_one(p, root)
    assert outcome.ok is True, outcome.reason
    assert src.exists()
    assert src.read_bytes() == b"original"
    assert p.status == "active"


def test_restore_refuses_to_clobber_existing_file(db, trash_env):
    roots_dir, trash_dir, write_file = trash_env
    root = _trash_root(db, roots_dir)
    user = make_user(db, username="u")
    write_file("a.jpg", b"v1")
    p = make_photo(db, root, rel_path="a.jpg", filename="a.jpg")
    db.commit()

    _move_to_trash(p, root, user)
    # User re-imported a different file at the same rel_path before
    # restoring. The restore must NOT silently overwrite it.
    write_file("a.jpg", b"v2-new")

    outcome = _restore_one(p, root)
    assert outcome.ok is False
    assert "이미" in (outcome.reason or "")
    # The replacement v2 stays intact.
    assert (roots_dir / "a.jpg").read_bytes() == b"v2-new"


# ----- permanent purge -----


def test_purge_deletes_files_and_row(db, trash_env):
    roots_dir, trash_dir, write_file = trash_env
    user = make_user(db, username="u")
    root = _trash_root(db, roots_dir)
    write_file("doomed.jpg", b"bye")
    p = make_photo(db, root, rel_path="doomed.jpg", filename="doomed.jpg")
    db.commit()

    _move_to_trash(p, root, user)
    p.status = "trashed"
    db.commit()
    photo_id = p.id

    ok = _purge_one(db, p)
    db.commit()
    assert ok is True

    # Dir for this photo is gone, DB row is gone.
    assert not (trash_dir / str(photo_id)).exists()
    assert db.get(Photo, photo_id) is None

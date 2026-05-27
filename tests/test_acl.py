"""Tests for app.auth_acl — the access-control layer.

Coverage focus:

- The new `UserAclSnapshot` (Phase B): equivalence with the per-photo
  helpers, longest-prefix folder match, default-level fallback, admin
  short-circuit.
- `require_photo_ids_level`: raises 403/404 at the right point, returns
  the level map on success, and uses the snapshot internally (we don't
  re-implement that here — equivalence with the SQL helpers is what
  matters).
- `apply_visible_photo_filter`: fast path (no folder ACL) and slow path
  (folder ACL) both filter `hidden` rows out while letting public /
  private-owned rows through.

What we deliberately don't test:
- Endpoint wiring (FastAPI dep injection, audit, sessions) — that's
  endpoint-level scaffolding, not the ACL logic.
- Permission *flags* on User (can_delete, etc.) — those are checked by
  auth.require_* decorators outside the ACL module.
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.auth_acl import (
    DEFAULT_LEVEL,
    UserAclSnapshot,
    apply_visible_photo_filter,
    effective_photo_level,
    effective_root_level,
    require_photo_ids_level,
    require_photo_level,
)
from app.models import Photo
from tests.conftest import (
    grant_folder,
    grant_root,
    make_photo,
    make_root,
    make_user,
)


# ----- effective_root_level / snapshot -----


def test_admin_snapshot_short_circuits_to_manage(db):
    admin = make_user(db, username="admin", is_admin=True)
    root = make_root(db)
    snap = UserAclSnapshot.for_user(db, admin)
    assert snap.is_admin is True
    # No matter what, admin gets manage.
    assert snap.level_for(root.id, "anything/here") == "manage"


def test_default_level_when_no_acl_rows(db):
    user = make_user(db, username="u")
    root = make_root(db)
    snap = UserAclSnapshot.for_user(db, user)
    # No row in either ACL table → DEFAULT_LEVEL.
    assert snap.level_for(root.id, "x.jpg") == DEFAULT_LEVEL


def test_root_acl_applies_when_no_folder_match(db):
    user = make_user(db, username="u")
    root = make_root(db)
    grant_root(db, root, user, "read")
    snap = UserAclSnapshot.for_user(db, user)
    assert snap.level_for(root.id, "any/path.jpg") == "read"


def test_folder_acl_overrides_root(db):
    user = make_user(db, username="u")
    root = make_root(db)
    grant_root(db, root, user, "hidden")
    grant_folder(db, root, user, "family/", "interact")
    snap = UserAclSnapshot.for_user(db, user)
    # Photo under the granted folder is visible …
    assert snap.level_for(root.id, "family/2024/a.jpg") == "interact"
    # … but a sibling path falls back to the hidden root ACL.
    assert snap.level_for(root.id, "private/b.jpg") == "hidden"


def test_longest_folder_prefix_wins(db):
    user = make_user(db, username="u")
    root = make_root(db)
    grant_folder(db, root, user, "trips/", "read")
    grant_folder(db, root, user, "trips/2024/", "contribute")
    snap = UserAclSnapshot.for_user(db, user)
    assert snap.level_for(root.id, "trips/2024/a.jpg") == "contribute"
    assert snap.level_for(root.id, "trips/2023/a.jpg") == "read"


def test_folder_prefix_not_substring_match(db):
    """'fam/' must NOT match 'family/...' — the trailing slash is
    deliberate. Catches the regression where the prefix matched mid-
    component because the rel_path got '/' appended only at the end."""
    user = make_user(db, username="u")
    root = make_root(db)
    grant_folder(db, root, user, "fam/", "hidden")
    snap = UserAclSnapshot.for_user(db, user)
    # Different top-level folder — must not be hidden.
    assert snap.level_for(root.id, "family/a.jpg") == DEFAULT_LEVEL


def test_snapshot_matches_per_photo_helpers(db):
    """Snapshot is a perf optimisation — its results must be
    identical to walking the single-photo helpers per id. If they
    diverge, bulk endpoints would silently authorise differently
    than single-photo endpoints. This is the canary."""
    user = make_user(db, username="u")
    root_a = make_root(db, label="A", abs_path="/a")
    root_b = make_root(db, label="B", abs_path="/b")
    grant_root(db, root_a, user, "read")
    grant_root(db, root_b, user, "hidden")
    grant_folder(db, root_b, user, "shared/", "interact")
    grant_folder(db, root_a, user, "secret/", "hidden")

    photos = [
        make_photo(db, root_a, rel_path="ordinary.jpg"),
        make_photo(db, root_a, rel_path="secret/x.jpg"),
        make_photo(db, root_b, rel_path="hidden-by-root.jpg"),
        make_photo(db, root_b, rel_path="shared/2024/a.jpg"),
    ]
    db.commit()
    snap = UserAclSnapshot.for_user(db, user)
    for p in photos:
        # Single-photo helper computes (folder | root | default) the
        # same way — equivalence here proves the SQL and Python
        # priority chains agree.
        single = effective_photo_level(db, user, p)
        bulk = snap.level_for(p.root_id, p.rel_path)
        assert single == bulk, f"divergence on {p.rel_path}: {single} vs {bulk}"


# ----- require_photo_ids_level (bulk gate) -----


def test_require_photo_ids_level_returns_map_on_pass(db):
    user = make_user(db, username="u")
    root = make_root(db)
    p1 = make_photo(db, root, rel_path="a.jpg")
    p2 = make_photo(db, root, rel_path="b.jpg")
    db.commit()
    out = require_photo_ids_level(db, user, [p1.id, p2.id], "interact")
    assert set(out.keys()) == {p1.id, p2.id}
    assert all(v == DEFAULT_LEVEL for v in out.values())


def test_require_photo_ids_level_hidden_raises_404(db):
    """`hidden` must respond 404, not 403 — leaking 403 tells the
    user the photo exists, defeating the point of hidden."""
    user = make_user(db, username="u")
    root = make_root(db)
    grant_root(db, root, user, "hidden")
    p = make_photo(db, root, rel_path="a.jpg")
    db.commit()
    with pytest.raises(HTTPException) as exc:
        require_photo_ids_level(db, user, [p.id], "read")
    assert exc.value.status_code == 404


def test_require_photo_ids_level_below_min_raises_403(db):
    user = make_user(db, username="u")
    root = make_root(db)
    grant_root(db, root, user, "read")
    p = make_photo(db, root, rel_path="a.jpg")
    db.commit()
    with pytest.raises(HTTPException) as exc:
        require_photo_ids_level(db, user, [p.id], "interact")
    assert exc.value.status_code == 403


def test_require_photo_ids_level_admin_shortcuts(db):
    admin = make_user(db, username="admin", is_admin=True)
    root = make_root(db)
    grant_root(db, root, admin, "hidden")  # ignored for admin
    p = make_photo(db, root, rel_path="a.jpg")
    db.commit()
    out = require_photo_ids_level(db, admin, [p.id], "manage")
    assert out == {p.id: "manage"}


# ----- single-photo gate (sanity, not part of the snapshot path) -----


def test_require_photo_level_passes_with_default(db):
    user = make_user(db, username="u")
    root = make_root(db)
    p = make_photo(db, root, rel_path="a.jpg")
    db.commit()
    require_photo_level(db, user, p, "read")  # no raise


def test_require_photo_level_hidden_root_raises_404(db):
    user = make_user(db, username="u")
    root = make_root(db)
    grant_root(db, root, user, "hidden")
    p = make_photo(db, root, rel_path="a.jpg")
    db.commit()
    with pytest.raises(HTTPException) as exc:
        require_photo_level(db, user, p, "read")
    assert exc.value.status_code == 404


# ----- apply_visible_photo_filter -----


def _visible_ids(db, user) -> set[int]:
    stmt = select(Photo.id)
    stmt = apply_visible_photo_filter(stmt, db, user)
    return {row[0] for row in db.execute(stmt).all()}


def test_filter_drops_hidden_root_photos_fast_path(db):
    """Fast path = no folder_acl rows at all. Hidden-root photos
    should drop out of the gallery query."""
    user = make_user(db, username="u")
    root_open = make_root(db, label="open", abs_path="/o")
    root_hidden = make_root(db, label="hidden", abs_path="/h")
    grant_root(db, root_hidden, user, "hidden")
    p_visible = make_photo(db, root_open, rel_path="a.jpg")
    p_hidden = make_photo(db, root_hidden, rel_path="b.jpg")
    db.commit()
    seen = _visible_ids(db, user)
    assert p_visible.id in seen
    assert p_hidden.id not in seen


def test_filter_admin_sees_everything(db):
    admin = make_user(db, username="admin", is_admin=True)
    root = make_root(db)
    grant_root(db, root, admin, "hidden")  # ignored
    p = make_photo(db, root, rel_path="a.jpg")
    db.commit()
    assert p.id in _visible_ids(db, admin)


def test_filter_private_photo_owner_visible(db):
    user = make_user(db, username="u")
    other = make_user(db, username="other")
    root = make_root(db)
    mine = make_photo(
        db, root, rel_path="mine.jpg",
        owner_user_id=user.id, visibility="private",
    )
    theirs = make_photo(
        db, root, rel_path="theirs.jpg",
        owner_user_id=other.id, visibility="private",
    )
    db.commit()
    seen = _visible_ids(db, user)
    assert mine.id in seen
    assert theirs.id not in seen


def test_filter_public_photo_pierces_hidden_root(db):
    """public visibility re-exposes a single photo from an otherwise
    hidden root."""
    user = make_user(db, username="u")
    root = make_root(db)
    grant_root(db, root, user, "hidden")
    pub = make_photo(db, root, rel_path="pub.jpg", visibility="public")
    priv = make_photo(db, root, rel_path="priv.jpg", visibility="inherit")
    db.commit()
    seen = _visible_ids(db, user)
    assert pub.id in seen
    assert priv.id not in seen


def test_filter_slow_path_folder_grant_visible(db):
    """Slow path = at least one folder_acl row exists for the user.
    The granted subfolder of a hidden root must still appear."""
    user = make_user(db, username="u")
    root = make_root(db)
    grant_root(db, root, user, "hidden")
    grant_folder(db, root, user, "shared/", "read")
    in_shared = make_photo(db, root, rel_path="shared/a.jpg")
    out_shared = make_photo(db, root, rel_path="other/b.jpg")
    db.commit()
    seen = _visible_ids(db, user)
    assert in_shared.id in seen
    assert out_shared.id not in seen


# ----- mid-fold regression: effective_root_level still works after snapshot landed -----


def test_effective_root_level_unchanged(db):
    user = make_user(db, username="u")
    root = make_root(db)
    grant_root(db, root, user, "contribute")
    assert effective_root_level(db, user, root.id) == "contribute"

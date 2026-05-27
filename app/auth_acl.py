"""Access-control level helpers (P2+).

Functions for computing a user's effective access level on a resource
and turning that level into either a SQL filter (for list queries) or
a guard (for mutating endpoints).

P2 only consults `root_acl`. P3 will extend the same helpers with
`folder_acl` (per-prefix override), P4 with `photos.visibility` (per-
photo private/public). The signatures here are designed to stay
stable across those additions — only the internals grow.

`is_admin` short-circuits everything: admins always read `manage` and
never get filtered out, regardless of any ACL row.
"""

from __future__ import annotations

from typing import Iterable, Literal

from fastapi import HTTPException, status
from sqlalchemy import Select, select
from sqlalchemy.orm import Session

from .models import Photo, RootACL, User


Level = Literal["hidden", "read", "interact", "contribute", "manage"]

# Ordered weakest → strongest. `_RANK` lets `>=` comparisons happen as
# integers, keeping `require_level()` and similar checks one-liners.
_LEVELS: list[Level] = ["hidden", "read", "interact", "contribute", "manage"]
_RANK: dict[Level, int] = {lvl: i for i, lvl in enumerate(_LEVELS)}

DEFAULT_LEVEL: Level = "read"
ADMIN_LEVEL: Level = "manage"


def level_at_least(actual: Level, minimum: Level) -> bool:
    return _RANK[actual] >= _RANK[minimum]


# ----- effective level lookups -----

def effective_root_level(db: Session, user: User, root_id: int) -> Level:
    """Compute the user's level on a given root_id.

    Admin always returns `manage`. Otherwise look for a `root_acl`
    row; if none, return the default of `read`.
    """
    if user.is_admin:
        return ADMIN_LEVEL
    row = db.get(RootACL, (root_id, user.id))
    return row.level if row else DEFAULT_LEVEL  # type: ignore[return-value]


def effective_photo_level(db: Session, user: User, photo: Photo) -> Level:
    """Compute the user's level on a specific photo.

    P2 only looks at the photo's root. P3 will layer folder ACL on top
    (longest path_prefix wins), P4 will layer photo.visibility.
    """
    if user.is_admin:
        return ADMIN_LEVEL
    return effective_root_level(db, user, photo.root_id)


# ----- SQL filter for list queries -----

def hidden_root_ids(db: Session, user: User) -> list[int]:
    """Root ids the user can NOT see (their ACL = 'hidden').

    Returned as a list so the caller can plug it into a `NOT IN`
    clause. Admins always get an empty list.
    """
    if user.is_admin:
        return []
    rows = db.execute(
        select(RootACL.root_id).where(
            RootACL.user_id == user.id,
            RootACL.level == "hidden",
        )
    ).scalars().all()
    return [int(r) for r in rows]


def apply_visible_photo_filter(stmt: Select, db: Session, user: User) -> Select:
    """Add a `Photo.root_id NOT IN (hidden roots for this user)` clause.

    Use this on any query that returns Photo rows (or aggregates over
    them) so hidden roots stop showing up in lists, maps, tag counts,
    histograms, etc. Returns the statement unchanged for admins (no
    join cost) and when the user has no hidden ACL rows.
    """
    hidden = hidden_root_ids(db, user)
    if not hidden:
        return stmt
    return stmt.where(~Photo.root_id.in_(hidden))


# ----- guards for single-photo / per-root operations -----

def _level_label_ko(lvl: Level) -> str:
    return {
        "hidden": "숨김",
        "read": "보기",
        "interact": "상호작용",
        "contribute": "기여",
        "manage": "관리",
    }[lvl]


def require_photo_level(
    db: Session, user: User, photo: Photo, minimum: Level,
) -> Level:
    """Raise 403 / 404 if the user's level on the photo is below the
    minimum. Returns the effective level on success so callers can
    branch on the exact tier without recomputing.

    `hidden`-level access (or below the minimum entirely) responds 404
    instead of 403 — leaking a 403 would tell the user the photo
    exists, defeating the point of `hidden`.
    """
    eff = effective_photo_level(db, user, photo)
    if eff == "hidden":
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    if not level_at_least(eff, minimum):
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            f"권한 없음: {_level_label_ko(minimum)} 이상 필요 "
            f"(현재 {_level_label_ko(eff)})",
        )
    return eff


def require_root_level(
    db: Session, user: User, root_id: int, minimum: Level,
) -> Level:
    """Same shape as require_photo_level but for root-level operations
    (folder create/rename/delete, upload, etc.).
    """
    eff = effective_root_level(db, user, root_id)
    if eff == "hidden":
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    if not level_at_least(eff, minimum):
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            f"권한 없음: {_level_label_ko(minimum)} 이상 필요 "
            f"(현재 {_level_label_ko(eff)})",
        )
    return eff


def require_photo_ids_level(
    db: Session, user: User, photo_ids: Iterable[int], minimum: Level,
) -> dict[int, Level]:
    """Bulk variant — checks every photo_id and returns a map from
    photo_id to effective level for those that pass. Photos at
    `hidden` or below the minimum raise 403 (one bad apple aborts the
    whole batch to keep callers honest about partial success).

    Useful for /bulk-delete, share creation, and similar batch ops.
    """
    if not photo_ids:
        return {}
    if user.is_admin:
        return {pid: ADMIN_LEVEL for pid in photo_ids}

    ids = list(set(int(p) for p in photo_ids))
    rows = db.execute(
        select(Photo.id, Photo.root_id).where(Photo.id.in_(ids))
    ).all()
    by_id = {int(r[0]): int(r[1]) for r in rows}
    hidden = set(hidden_root_ids(db, user))

    out: dict[int, Level] = {}
    for pid in ids:
        root_id = by_id.get(pid)
        if root_id is None:
            # Caller passed an unknown id — let the downstream code 404.
            continue
        if root_id in hidden:
            raise HTTPException(
                status.HTTP_404_NOT_FOUND,
                f"photo {pid} not found",
            )
        eff = effective_root_level(db, user, root_id)
        if not level_at_least(eff, minimum):
            raise HTTPException(
                status.HTTP_403_FORBIDDEN,
                f"권한 없음 (photo {pid}): "
                f"{_level_label_ko(minimum)} 이상 필요",
            )
        out[pid] = eff
    return out

"""Access-control level helpers (P2+).

Functions for computing a user's effective access level on a resource
and turning that level into either a SQL filter (for list queries) or
a guard (for mutating endpoints).

Phases consulted:
- P2 (root_acl)   : per-root level
- P3 (folder_acl) : per-prefix override (longest match wins)
- P4 (photos.visibility) — added later; private overrides everything
  (owner+admin only), public forces level=read on top of everything

`is_admin` short-circuits everything: admins always read `manage` and
never get filtered out, regardless of any ACL row.
"""

from __future__ import annotations

from typing import Iterable, Literal

from fastapi import HTTPException, status
from sqlalchemy import Select, func, or_, select
from sqlalchemy.orm import Session

from .models import FolderACL, Photo, RootACL, User


Level = Literal["hidden", "read", "interact", "contribute", "manage"]

# Ordered weakest → strongest. `_RANK` lets `>=` comparisons happen as
# integers, keeping `require_level()` and similar checks one-liners.
_LEVELS: list[Level] = ["hidden", "read", "interact", "contribute", "manage"]
_RANK: dict[Level, int] = {lvl: i for i, lvl in enumerate(_LEVELS)}

DEFAULT_LEVEL: Level = "interact"
ADMIN_LEVEL: Level = "manage"


def level_at_least(actual: Level, minimum: Level) -> bool:
    return _RANK[actual] >= _RANK[minimum]


# ----- effective level lookups -----

def effective_root_level(db: Session, user: User, root_id: int) -> Level:
    """Compute the user's level on a given root_id (P2 only — folder
    overrides are ignored here, see effective_photo_level for the
    full hierarchy).

    Admin always returns `manage`. Otherwise look for a `root_acl`
    row; if none, return the default of `interact` — every family
    member can browse + rate + comment without setup, but editing
    shared metadata (tags / caption / date) or deleting requires an
    explicit ACL grant.
    """
    if user.is_admin:
        return ADMIN_LEVEL
    row = db.get(RootACL, (root_id, user.id))
    return row.level if row else DEFAULT_LEVEL  # type: ignore[return-value]


def _matching_folder_level(
    db: Session, user: User, root_id: int, rel_path: str,
) -> Level | None:
    """Find the longest folder_acl prefix matching this photo's path
    for this user, and return its level. None means no folder_acl
    matches — caller falls back to root_acl.
    """
    row = db.execute(
        select(FolderACL.level)
        .where(
            FolderACL.user_id == user.id,
            FolderACL.root_id == root_id,
            # path_prefix comes with a trailing slash, so LIKE prefix||'%'
            # gives unambiguous "this folder or under it" matching.
            (rel_path + "/").like(FolderACL.path_prefix + "%"),
        )
        .order_by(func.length(FolderACL.path_prefix).desc())
        .limit(1)
    ).scalar_one_or_none()
    return row  # type: ignore[return-value]


def effective_photo_level(db: Session, user: User, photo: Photo) -> Level:
    """Compute the user's level on a specific photo.

    Priority: folder_acl (longest matching prefix) > root_acl > default.
    Admin always returns `manage` without touching either table.
    """
    if user.is_admin:
        return ADMIN_LEVEL
    fl = _matching_folder_level(db, user, photo.root_id, photo.rel_path or "")
    if fl is not None:
        return fl
    return effective_root_level(db, user, photo.root_id)


# ----- SQL filter for list queries -----

def hidden_root_ids(db: Session, user: User) -> list[int]:
    """Root ids the user can NOT see at the root level (root_acl =
    'hidden'). Note: a hidden root can still expose individual
    subfolders if a folder_acl row re-grants `read` or higher — see
    apply_visible_photo_filter for the combined logic.

    Admins always get an empty list.
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


def _has_any_folder_acl(db: Session, user: User) -> bool:
    """Cheap existence check used to decide whether to bother with the
    expensive folder-level visibility filter at all."""
    if user.is_admin:
        return False
    return db.execute(
        select(FolderACL.root_id).where(FolderACL.user_id == user.id).limit(1)
    ).first() is not None


def apply_visible_photo_filter(stmt: Select, db: Session, user: User) -> Select:
    """Drop photos the user can't see (effective level = 'hidden')
    from the query result.

    Fast path — admin, or user with no ACL rows at all → no filter.
    Mid path — user has root_acl rows but no folder_acl → simple
        `NOT IN` on hidden root ids (P2 behavior).
    Slow path — user has folder_acl rows → CASE/COALESCE that picks
        the longest matching folder prefix per photo, falling back to
        the root level, falling back to the default. Slightly heavier
        SQL but only a handful of family users ever take this path
        and folder_acl tables stay tiny.
    """
    if user.is_admin:
        return stmt
    has_folder = _has_any_folder_acl(db, user)
    if not has_folder:
        hidden = hidden_root_ids(db, user)
        if not hidden:
            return stmt
        return stmt.where(~Photo.root_id.in_(hidden))

    # Folder-level path. For each photo, the effective level is:
    #   COALESCE(longest matching folder_acl level,
    #            root_acl level,
    #            'read')
    folder_level_sub = (
        select(FolderACL.level)
        .where(
            FolderACL.user_id == user.id,
            FolderACL.root_id == Photo.root_id,
            (Photo.rel_path + "/").like(FolderACL.path_prefix + "%"),
        )
        .order_by(func.length(FolderACL.path_prefix).desc())
        .limit(1)
        .correlate(Photo)
        .scalar_subquery()
    )
    root_level_sub = (
        select(RootACL.level)
        .where(
            RootACL.user_id == user.id,
            RootACL.root_id == Photo.root_id,
        )
        .correlate(Photo)
        .scalar_subquery()
    )
    effective = func.coalesce(folder_level_sub, root_level_sub, "read")
    return stmt.where(effective != "hidden")


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
    """Root-level guard — looks only at root_acl, ignoring any folder
    overrides. Use for ops that target the whole root (e.g. enabling
    a root, root rename) where folder-level overrides shouldn't help.

    For folder-bound ops (create/rename/delete subfolder, upload
    into a folder), use require_folder_level so the user can act
    inside a folder where they were re-granted access even if the
    root is hidden.
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


def effective_folder_level(
    db: Session, user: User, root_id: int, rel_path: str,
) -> Level:
    """Compute the effective level for a folder path (no Photo row
    required). Mirror of effective_photo_level for folder ops.
    """
    if user.is_admin:
        return ADMIN_LEVEL
    fl = _matching_folder_level(db, user, root_id, rel_path or "")
    if fl is not None:
        return fl
    return effective_root_level(db, user, root_id)


def require_folder_level(
    db: Session, user: User, root_id: int, rel_path: str, minimum: Level,
) -> Level:
    """Like require_root_level but consults folder_acl too. Use for
    operations on a specific folder path (folder create/rename/delete,
    upload). `rel_path` is the folder path (no trailing slash, '' for
    the root itself).
    """
    eff = effective_folder_level(db, user, root_id, rel_path)
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
    `hidden` or below the minimum raise 403/404 (one bad apple aborts
    the whole batch to keep callers honest about partial success).

    Uses the full folder-aware effective level so re-granted folders
    inside a hidden root still pass.

    Useful for /bulk-delete, share creation, and similar batch ops.
    """
    if not photo_ids:
        return {}
    if user.is_admin:
        return {int(pid): ADMIN_LEVEL for pid in photo_ids}

    ids = list(set(int(p) for p in photo_ids))
    rows = db.execute(
        select(Photo.id, Photo.root_id, Photo.rel_path).where(Photo.id.in_(ids))
    ).all()
    info = {int(r[0]): (int(r[1]), r[2] or "") for r in rows}

    out: dict[int, Level] = {}
    for pid in ids:
        meta = info.get(pid)
        if meta is None:
            # Caller passed an unknown id — let the downstream code 404.
            continue
        root_id, rel_path = meta
        # Folder ACL first (longest matching prefix), root_acl fallback.
        fl = _matching_folder_level(db, user, root_id, rel_path)
        eff: Level = fl if fl is not None else effective_root_level(db, user, root_id)
        if eff == "hidden":
            raise HTTPException(
                status.HTTP_404_NOT_FOUND,
                f"photo {pid} not found",
            )
        if not level_at_least(eff, minimum):
            raise HTTPException(
                status.HTTP_403_FORBIDDEN,
                f"권한 없음 (photo {pid}): "
                f"{_level_label_ko(minimum)} 이상 필요",
            )
        out[pid] = eff
    return out

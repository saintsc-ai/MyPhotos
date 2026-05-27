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

from dataclasses import dataclass, field
from typing import Iterable, Literal

from fastapi import HTTPException, status
from sqlalchemy import Select, func, literal, or_, select
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


# ----- batched snapshot for bulk operations -----

@dataclass
class UserAclSnapshot:
    """A user's full root_acl + folder_acl pulled into memory in two
    queries so per-photo level lookups are O(prefix_count) Python work
    instead of two SQL round-trips each.

    Built once at the top of bulk endpoints (delete, tag, share-create…)
    and reused for every photo in the batch. Cuts a 1000-photo bulk-op
    from ~2001 queries to 3 (photo metadata + root_acl + folder_acl).

    Admin callers get an empty snapshot whose level_for() always
    returns ADMIN_LEVEL — kept as a sentinel so call sites don't have
    to branch on `is_admin` themselves.
    """

    is_admin: bool
    # root_id → level. Missing key means no explicit grant → default.
    root_levels: dict[int, Level] = field(default_factory=dict)
    # root_id → list[(path_prefix, level)] sorted longest-first so the
    # first match in iteration order is the longest match (mirrors the
    # `ORDER BY length(path_prefix) DESC` of the SQL fallback path).
    folder_rules: dict[int, list[tuple[str, Level]]] = field(default_factory=dict)

    @classmethod
    def for_user(cls, db: Session, user: User) -> "UserAclSnapshot":
        if user.is_admin:
            return cls(is_admin=True)
        root_rows = db.execute(
            select(RootACL.root_id, RootACL.level).where(RootACL.user_id == user.id)
        ).all()
        folder_rows = db.execute(
            select(FolderACL.root_id, FolderACL.path_prefix, FolderACL.level)
            .where(FolderACL.user_id == user.id)
        ).all()
        roots: dict[int, Level] = {int(r[0]): r[1] for r in root_rows}  # type: ignore[misc]
        folders: dict[int, list[tuple[str, Level]]] = {}
        for rid, prefix, lvl in folder_rows:
            folders.setdefault(int(rid), []).append((str(prefix), lvl))  # type: ignore[arg-type]
        # Pre-sort each root's prefix list longest-first so level_for can
        # short-circuit on the first match.
        for rules in folders.values():
            rules.sort(key=lambda r: len(r[0]), reverse=True)
        return cls(is_admin=False, root_levels=roots, folder_rules=folders)

    def _folder_match(self, root_id: int, rel_path: str) -> Level | None:
        rules = self.folder_rules.get(root_id)
        if not rules:
            return None
        # path_prefix in the DB always ends with '/'. Mirror the SQL
        # `(rel_path || '/') LIKE prefix || '%'` semantics by giving
        # rel_path a trailing slash before comparing.
        haystack = (rel_path or "") + "/"
        for prefix, lvl in rules:
            if haystack.startswith(prefix):
                return lvl
        return None

    def level_for(self, root_id: int, rel_path: str) -> Level:
        """Effective level on a (root, path) — folder ACL first
        (longest match), then root ACL, then the global default. Admin
        short-circuits to ADMIN_LEVEL.
        """
        if self.is_admin:
            return ADMIN_LEVEL
        fl = self._folder_match(root_id, rel_path)
        if fl is not None:
            return fl
        return self.root_levels.get(root_id, DEFAULT_LEVEL)


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
    # rel_path here is a plain Python str — wrap it as a SQL literal so
    # .like() resolves to a column expression instead of being looked
    # up on the str object (which raised
    # `AttributeError: 'str' object has no attribute 'like'`).
    path_lit = literal(rel_path + "/")
    row = db.execute(
        select(FolderACL.level)
        .where(
            FolderACL.user_id == user.id,
            FolderACL.root_id == root_id,
            # path_prefix comes with a trailing slash, so LIKE prefix||'%'
            # gives unambiguous "this folder or under it" matching.
            path_lit.like(FolderACL.path_prefix + "%"),
        )
        .order_by(func.length(FolderACL.path_prefix).desc())
        .limit(1)
    ).scalar_one_or_none()
    return row  # type: ignore[return-value]


def effective_photo_level(db: Session, user: User, photo: Photo) -> Level:
    """Compute the user's level on a specific photo.

    Priority:
      1. photo.visibility == 'private' → owner + admin only, else hidden
      2. folder_acl (longest matching prefix)
      3. root_acl
      4. default (interact)

    photo.visibility == 'public' acts as a floor: whatever the
    folder/root layers say, the effective level becomes at least
    `read` (i.e. hidden gets bumped to read).

    Admin always returns `manage` without touching any table.
    """
    if user.is_admin:
        return ADMIN_LEVEL

    vis = getattr(photo, "visibility", "inherit") or "inherit"
    if vis == "private":
        owner_id = getattr(photo, "owner_user_id", None)
        if owner_id is None or owner_id != user.id:
            return "hidden"
        return ADMIN_LEVEL    # owner gets full manage on their own private photo

    fl = _matching_folder_level(db, user, photo.root_id, photo.rel_path or "")
    base: Level = fl if fl is not None else effective_root_level(db, user, photo.root_id)

    if vis == "public" and base == "hidden":
        return "read"   # public floor: re-expose at least at read
    return base


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

    Visibility (P4) is layered on top of ACL:
    - private photos: only the owner sees them (and admin via the
      early short-circuit at the top)
    - public photos: bypass ACL hidden entirely
    - inherit: fall through to folder_acl / root_acl
    """
    if user.is_admin:
        return stmt

    # Visibility predicates evaluated first so the optimizer can drop
    # private-not-mine photos before doing any ACL joins.
    #   keep if visibility = 'public'
    #   keep if visibility = 'private' AND owner_user_id = me
    #   else evaluate ACL
    has_folder = _has_any_folder_acl(db, user)
    hidden_roots = [] if has_folder else hidden_root_ids(db, user)

    if not has_folder:
        # Mid/fast path. Conditions:
        #   visibility=public                                          → keep
        #   visibility=private AND owner=me                            → keep
        #   visibility IS NULL or 'inherit' AND root NOT IN hidden     → keep
        #
        # NULL visibility is treated as 'inherit' because the DEFAULT
        # backfill on the alembic 0015 batch recreate doesn't always
        # land cleanly (some SQLite + alembic combos leave the new
        # column NULL on existing rows). The fast path now matches
        # whatever effective_photo_level does.
        inherit_or_null = or_(
            Photo.visibility == "inherit",
            Photo.visibility.is_(None),
        )
        if hidden_roots:
            inherit_clause = inherit_or_null & Photo.root_id.notin_(hidden_roots)
        else:
            inherit_clause = inherit_or_null
        cond = or_(
            Photo.visibility == "public",
            (Photo.visibility == "private") & (Photo.owner_user_id == user.id),
            inherit_clause,
        )
        return stmt.where(cond)

    # Slow path — folder ACL in play. For each photo, the effective
    # level is COALESCE(folder_acl_level, root_acl_level, default).
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
    effective = func.coalesce(folder_level_sub, root_level_sub, DEFAULT_LEVEL)
    acl_visible = effective != "hidden"
    return stmt.where(
        or_(
            Photo.visibility == "public",
            (Photo.visibility == "private") & (Photo.owner_user_id == user.id),
            (Photo.visibility == "inherit") & acl_visible,
        )
    )


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

    # One round-trip pulls every root_acl + folder_acl row for this
    # user. Per-photo level resolution then runs entirely in Python —
    # without this, a 1000-photo bulk-delete fires ~2000 queries
    # (folder_acl + root_acl per photo).
    snapshot = UserAclSnapshot.for_user(db, user)

    out: dict[int, Level] = {}
    for pid in ids:
        meta = info.get(pid)
        if meta is None:
            # Caller passed an unknown id — let the downstream code 404.
            continue
        root_id, rel_path = meta
        eff = snapshot.level_for(root_id, rel_path)
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

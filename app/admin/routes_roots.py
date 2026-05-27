"""Admin endpoints for managing photo roots.

Roots are stored in the DB (not config) so the UI can add/edit/remove them.
Skeleton in MVP 1 — scan trigger and full validation come in MVP 2.
"""

from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .. import audit
from ..api.deps import get_db
from ..auth import require_auth
from ..auth_acl import DEFAULT_LEVEL
from ..models import Root, RootACL, User

router = APIRouter(prefix="/admin/roots", tags=["admin", "roots"])


class RootIn(BaseModel):
    label: str = Field(min_length=1, max_length=64, pattern=r"^[a-zA-Z0-9_\-]+$")
    abs_path: str = Field(min_length=1)
    readonly: bool = True
    enabled: bool = True
    notes: str | None = None
    # POSIX-style relative paths under abs_path that the scanner skips
    # and the gallery hides. Cleaned + JSON-serialised on save.
    ignore_paths: list[str] | None = None


class RootPatch(BaseModel):
    abs_path: str | None = None
    readonly: bool | None = None
    enabled: bool | None = None
    notes: str | None = None
    # Auto-rescan period in seconds. Worker checks every 10 minutes
    # and enqueues a discover_root if last_full_scan is older than
    # this. Floor at 60 s; no ceiling (set 'enabled=false' to stop
    # auto scans entirely).
    scan_interval: int | None = Field(None, ge=60)
    # Replace the ignore-path list. Pass [] to clear; omit to leave
    # unchanged.
    ignore_paths: list[str] | None = None


class RootOut(BaseModel):
    id: int
    label: str
    abs_path: str
    readonly: bool
    enabled: bool
    scan_interval: int
    last_full_scan: datetime | None
    last_event_at: datetime | None
    notes: str | None
    created_at: datetime
    # Liveness — checked on read so the UI can show warnings.
    exists: bool
    readable: bool
    # Cleaned list (not the raw JSON text) so the UI never has to
    # parse it.
    ignore_paths: list[str] = Field(default_factory=list)

    class Config:
        from_attributes = True


def _augment(root: Root) -> dict:
    from ..scanner.utils import root_ignore_paths
    p = Path(root.abs_path)
    return {
        **{c.name: getattr(root, c.name) for c in root.__table__.columns},
        "exists": p.exists(),
        "readable": p.exists() and os.access(p, os.R_OK),
        # Replace the raw JSON text with the parsed list — RootOut
        # declares ignore_paths as list[str].
        "ignore_paths": root_ignore_paths(root),
    }


def _validate_path(abs_path: str) -> Path:
    p = Path(abs_path)
    if not p.is_absolute():
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, "abs_path must be an absolute path"
        )
    # Existence is reported as a flag rather than enforced — useful when porting:
    # paths may temporarily not exist while a volume is being mounted.
    return p


@router.get("", response_model=list[RootOut])
def list_roots(db: Session = Depends(get_db)) -> list[RootOut]:
    rows = db.execute(select(Root).order_by(Root.id)).scalars().all()
    return [RootOut(**_augment(r)) for r in rows]


@router.post("", response_model=RootOut, status_code=status.HTTP_201_CREATED)
def create_root(body: RootIn, db: Session = Depends(get_db)) -> RootOut:
    from ..scanner.utils import serialize_ignore_paths
    _validate_path(body.abs_path)
    root = Root(
        label=body.label,
        abs_path=body.abs_path,
        readonly=body.readonly,
        enabled=body.enabled,
        notes=body.notes,
        ignore_paths=serialize_ignore_paths(body.ignore_paths or []),
    )
    db.add(root)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, f"label '{body.label}' already exists")
    db.refresh(root)
    return RootOut(**_augment(root))


@router.patch("/{root_id}", response_model=RootOut)
def update_root(root_id: int, body: RootPatch, db: Session = Depends(get_db)) -> RootOut:
    root = db.get(Root, root_id)
    if root is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    if body.abs_path is not None:
        _validate_path(body.abs_path)
        root.abs_path = body.abs_path
    if body.readonly is not None:
        root.readonly = body.readonly
    if body.enabled is not None:
        root.enabled = body.enabled
    if body.notes is not None:
        root.notes = body.notes
    if body.scan_interval is not None:
        root.scan_interval = body.scan_interval
    ignore_changed = False
    if body.ignore_paths is not None:
        from ..scanner.utils import serialize_ignore_paths
        root.ignore_paths = serialize_ignore_paths(body.ignore_paths)
        ignore_changed = True
    db.commit()
    # Apply the ignore-list sweep right away so the change shows up in
    # the gallery without waiting for the next discover_root. No
    # filesystem walk needed — it's pure SQL.
    if ignore_changed:
        from ..scanner.discover import apply_ignore_sweep
        apply_ignore_sweep(db, root)
    db.refresh(root)
    return RootOut(**_augment(root))


@router.delete("/{root_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_root(root_id: int, db: Session = Depends(get_db)) -> None:
    root = db.get(Root, root_id)
    if root is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    db.delete(root)
    db.commit()


class ScanResponse(BaseModel):
    job_id: int
    root_id: int


@router.post("/{root_id}/scan", response_model=ScanResponse, status_code=status.HTTP_202_ACCEPTED)
def trigger_scan(
    root_id: int,
    limit: int | None = None,
    db: Session = Depends(get_db),
) -> ScanResponse:
    """Enqueue a discover_root job. The worker picks it up and walks
    the root, registering new files and queueing per-file index jobs.
    """
    from ..worker.jobs import enqueue

    root = db.get(Root, root_id)
    if root is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    if not root.enabled:
        raise HTTPException(status.HTTP_409_CONFLICT, "root is disabled")
    payload: dict = {"root_id": root_id}
    if limit is not None:
        payload["limit"] = int(limit)
    job_id = enqueue(db, kind="discover_root", payload=payload, priority=20)
    db.commit()
    return ScanResponse(job_id=job_id, root_id=root_id)


# ---------- ACL (P2 of access control) ----------
#
# Admin assigns each non-admin user a level on each root. Absence of a
# row = default `read`. Admin users always behave as `manage` (the
# server bypasses these rows when computing effective level), but
# storing a row for them is harmless if the UI happens to write one.

_ACL_LEVELS = ("hidden", "read", "interact", "contribute", "manage")


class RootACLEntryOut(BaseModel):
    user_id: int
    username: str
    is_admin: bool
    level: str   # hidden / read / interact / contribute / manage
    is_default: bool   # true when there's no row (effective = 'read')


class RootACLEntryIn(BaseModel):
    user_id: int
    level: str = Field(pattern=r"^(hidden|read|interact|contribute|manage)$")


@router.get("/{root_id}/acl", response_model=list[RootACLEntryOut])
def list_root_acl(
    root_id: int, db: Session = Depends(get_db),
) -> list[RootACLEntryOut]:
    """One row per user — explicit ACL rows come from `root_acl`,
    the rest fall through to the `read` default. The UI shows the
    full user list so the admin can flip levels without first
    creating rows.
    """
    if db.get(Root, root_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "root not found")
    users = db.execute(
        select(User).order_by(User.id)
    ).scalars().all()
    rows = db.execute(
        select(RootACL).where(RootACL.root_id == root_id)
    ).scalars().all()
    by_user = {r.user_id: r.level for r in rows}
    out: list[RootACLEntryOut] = []
    for u in users:
        lvl = by_user.get(u.id)
        out.append(RootACLEntryOut(
            user_id=u.id,
            username=u.username,
            is_admin=bool(u.is_admin),
            level=lvl if lvl else DEFAULT_LEVEL,
            is_default=(lvl is None),
        ))
    return out


@router.put("/{root_id}/acl", status_code=status.HTTP_204_NO_CONTENT)
def set_root_acl(
    root_id: int,
    entry: RootACLEntryIn,
    user: User = Depends(require_auth),
    db: Session = Depends(get_db),
) -> None:
    """Upsert a single (root_id, user_id) ACL row. Sending the default
    level (`read`) doesn't delete the row — admin who explicitly sets
    `read` is opting *in* to having a row, which makes it visible in
    the listing. Use DELETE to revert to the implicit default.
    """
    if db.get(Root, root_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "root not found")
    target = db.get(User, entry.user_id)
    if target is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "user not found")
    if entry.level not in _ACL_LEVELS:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "invalid level")
    existing = db.get(RootACL, (root_id, entry.user_id))
    before = existing.level if existing else None
    if existing is None:
        db.add(RootACL(
            root_id=root_id, user_id=entry.user_id, level=entry.level,
        ))
    else:
        existing.level = entry.level
    audit.record(
        db, user, "acl.root.set", "root", root_id,
        detail={"target_user": target.username, "target_id": target.id,
                "before": before, "after": entry.level},
    )
    db.commit()


@router.delete("/{root_id}/acl/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_root_acl(
    root_id: int, user_id: int,
    user: User = Depends(require_auth),
    db: Session = Depends(get_db),
) -> None:
    """Drop the explicit ACL row for (root_id, user_id) — the user
    falls back to the default `read`.
    """
    row = db.get(RootACL, (root_id, user_id))
    if row is None:
        return  # idempotent
    target = db.get(User, user_id)
    audit.record(
        db, user, "acl.root.delete", "root", root_id,
        detail={"target_user": target.username if target else None,
                "target_id": user_id, "before": row.level},
    )
    db.delete(row)
    db.commit()

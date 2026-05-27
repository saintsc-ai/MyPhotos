"""Audit logging — append-only record of privileged actions (P5).

One function: `record(...)`. Callers stage a row on the existing
session; the surrounding endpoint commits, so the audit row joins
the user's action transactionally (no half-committed log line for an
operation that rolled back).

Action vocabulary (informal — add new strings as needed):
    photo.trash      — photo sent to trash (resource_id = photo id)
    photo.restore    — photo restored from trash
    photo.purge      — photo permanently deleted
    photo.visibility — visibility flipped (detail = before/after)
    share.create     — share created (detail = photo count, title)
    share.revoke     — share soft-revoked
    share.purge      — share hard-deleted
    acl.root.set     — root_acl row upserted (detail = level)
    acl.root.delete  — root_acl row deleted
    acl.folder.set   — folder_acl row upserted
    acl.folder.delete— folder_acl row deleted
    user.create      — new user account
    user.update      — flag / admin change on a user
    user.delete      — user account removed
    folder.create / folder.rename / folder.delete / folder.upload
    root.create / root.update / root.delete

Helpers below wrap the most common shapes so call sites stay tidy.
"""

from __future__ import annotations

import json
from typing import Any, Optional

from sqlalchemy.orm import Session

from .models import AuditLog, User


def record(
    db: Session,
    user: Optional[User],
    action: str,
    resource_type: str,
    resource_id: Optional[str | int] = None,
    detail: Optional[Any] = None,
) -> None:
    """Stage one audit_log row on the session.

    `user` may be None for system-driven events (worker, watcher).
    `detail` accepts any JSON-serialisable object; non-dict values
    are wrapped as {"value": x}. The caller's existing commit
    persists everything together.
    """
    detail_json: Optional[str] = None
    if detail is not None:
        if isinstance(detail, str):
            detail_json = detail
        else:
            try:
                detail_json = json.dumps(detail, ensure_ascii=False, default=str)
            except (TypeError, ValueError):
                detail_json = repr(detail)

    row = AuditLog(
        user_id=user.id if user is not None else None,
        username=user.username if user is not None else None,
        action=action,
        resource_type=resource_type,
        resource_id=str(resource_id) if resource_id is not None else None,
        detail=detail_json,
    )
    db.add(row)


def purge_older_than_days(db: Session, days: int = 90) -> int:
    """Delete audit_log rows older than `days`. Returns the count
    removed. Designed to be called from the worker tick.
    """
    if days <= 0:
        return 0
    from sqlalchemy import delete as _delete, text as _text
    res = db.execute(
        _delete(AuditLog).where(
            AuditLog.ts < _text(f"datetime('now', '-{int(days)} days')")
        )
    )
    n = res.rowcount or 0
    if n:
        db.commit()
    return int(n)

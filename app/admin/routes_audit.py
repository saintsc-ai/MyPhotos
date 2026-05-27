"""Admin audit-log read endpoints (P5).

Append-only history of privileged actions. The audit_log table is
populated by app.audit.record() called from each mutating endpoint;
this module is the read side — list with pagination + filters and
a one-shot purge for "older than N days".
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, Query, status
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import audit as audit_helper
from ..api.deps import get_db
from ..auth import require_admin
from ..models import AuditLog, User

router = APIRouter(prefix="/admin/audit", tags=["admin", "audit"])


class AuditOut(BaseModel):
    id: int
    ts: datetime
    user_id: Optional[int]
    username: Optional[str]
    action: str
    resource_type: str
    resource_id: Optional[str]
    detail: Optional[dict | str] = None

    class Config:
        from_attributes = True


class AuditPage(BaseModel):
    total: int
    page: int
    page_size: int
    items: list[AuditOut]


def _decode_detail(s: Optional[str]) -> Optional[dict | str]:
    if not s:
        return None
    try:
        return json.loads(s)
    except (ValueError, TypeError):
        return s


@router.get("", response_model=AuditPage)
def list_audit(
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=500),
    user_id: int | None = Query(None, description="filter by acting user"),
    action: str | None = Query(None, description="exact action match"),
    resource_type: str | None = Query(
        None, description="photo / share / root / folder / user / acl"
    ),
    resource_id: str | None = Query(None, description="exact resource id"),
    since: datetime | None = Query(None, description="ts >= this"),
    until: datetime | None = Query(None, description="ts <= this"),
    _: User = Depends(require_admin),
    db: Session = Depends(get_db),
) -> AuditPage:
    """Paginated list of audit_log rows, newest first. All filters
    chain as AND."""
    q = select(AuditLog)
    if user_id is not None:
        q = q.where(AuditLog.user_id == user_id)
    if action:
        q = q.where(AuditLog.action == action)
    if resource_type:
        q = q.where(AuditLog.resource_type == resource_type)
    if resource_id:
        q = q.where(AuditLog.resource_id == resource_id)
    if since:
        q = q.where(AuditLog.ts >= since)
    if until:
        q = q.where(AuditLog.ts <= until)

    total = db.execute(
        select(func.count()).select_from(q.subquery())
    ).scalar_one()

    rows = db.execute(
        q.order_by(AuditLog.ts.desc(), AuditLog.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    ).scalars().all()

    return AuditPage(
        total=int(total or 0),
        page=page,
        page_size=page_size,
        items=[
            AuditOut(
                id=r.id, ts=r.ts, user_id=r.user_id, username=r.username,
                action=r.action, resource_type=r.resource_type,
                resource_id=r.resource_id, detail=_decode_detail(r.detail),
            )
            for r in rows
        ],
    )


class PurgeIn(BaseModel):
    days: int = 90


class PurgeOut(BaseModel):
    deleted: int
    days: int


@router.post("/purge", response_model=PurgeOut)
def purge_audit(
    body: PurgeIn,
    _: User = Depends(require_admin),
    db: Session = Depends(get_db),
) -> PurgeOut:
    """Hard-delete audit_log rows older than N days. The same logic
    runs from the worker tick automatically — this endpoint is for
    manual one-shot cleanup or testing the retention window.
    """
    n = audit_helper.purge_older_than_days(db, body.days)
    return PurgeOut(deleted=n, days=body.days)

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

from ..api.deps import get_db
from ..models import Root

router = APIRouter(prefix="/admin/roots", tags=["admin", "roots"])


class RootIn(BaseModel):
    label: str = Field(min_length=1, max_length=64, pattern=r"^[a-zA-Z0-9_\-]+$")
    abs_path: str = Field(min_length=1)
    readonly: bool = True
    enabled: bool = True
    notes: str | None = None


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

    class Config:
        from_attributes = True


def _augment(root: Root) -> dict:
    p = Path(root.abs_path)
    return {
        **{c.name: getattr(root, c.name) for c in root.__table__.columns},
        "exists": p.exists(),
        "readable": p.exists() and os.access(p, os.R_OK),
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
    _validate_path(body.abs_path)
    root = Root(
        label=body.label,
        abs_path=body.abs_path,
        readonly=body.readonly,
        enabled=body.enabled,
        notes=body.notes,
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
    db.commit()
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

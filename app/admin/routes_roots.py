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
    # Attribute uploads to the user named by the first path segment
    # (<root>/<username>/…) when they don't come through /upload. For
    # external drop folders like PhotoSync-over-SMB. See Root model.
    owner_from_subfolder: bool = False
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
    owner_from_subfolder: bool | None = None
    # Replace the ignore-path list. Pass [] to clear; omit to leave
    # unchanged.
    ignore_paths: list[str] | None = None


class RootOut(BaseModel):
    id: int
    label: str
    abs_path: str
    readonly: bool
    enabled: bool
    owner_from_subfolder: bool
    scan_interval: int
    last_full_scan: datetime | None
    last_event_at: datetime | None
    notes: str | None
    created_at: datetime
    # Liveness — checked on read so the UI can show warnings.
    exists: bool
    readable: bool
    # Same os.access probe but for write — the answer the rotate/delete
    # paths actually need. When false the admin should expect those ops
    # to fail unless the root is intentionally marked readonly.
    writable: bool
    write_hint: str | None = None
    # Cleaned list (not the raw JSON text) so the UI never has to
    # parse it.
    ignore_paths: list[str] = Field(default_factory=list)

    class Config:
        from_attributes = True


def _augment(root: Root) -> dict:
    from ..scanner.utils import root_ignore_paths
    p = Path(root.abs_path)
    exists = p.exists()
    readable = exists and os.access(p, os.R_OK)
    writable = exists and os.access(p, os.W_OK)
    # User-facing hint when permissions look wrong. Only set when we
    # have something useful to say — None for "all good".
    write_hint: str | None = None
    if not exists:
        write_hint = "경로가 존재하지 않습니다"
    elif not readable:
        write_hint = (
            "이 폴더는 읽을 수 없습니다 — 색인이 동작하지 않습니다. "
            "권한을 풀어주세요 (README 9단계의 'scripts/fix-photo-perms.sh'). "
        )
    elif not writable:
        write_hint = (
            "이 폴더는 색인은 되지만 회전·삭제(휴지통)는 불가합니다. "
            "쓰기 권한이 필요하면 sudo ./scripts/fix-photo-perms.sh "
            "실행 (README 9단계 참고)."
        )
    return {
        **{c.name: getattr(root, c.name) for c in root.__table__.columns},
        "exists": exists,
        "readable": readable,
        "writable": writable,
        "write_hint": write_hint,
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
        owner_from_subfolder=body.owner_from_subfolder,
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
    if body.owner_from_subfolder is not None:
        root.owner_from_subfolder = body.owner_from_subfolder
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


# ---------- GPS estimation ----------


class LocationEstimateStats(BaseModel):
    # Total photos in the root that carry a taken_at — denominator for
    # the progress card.
    total_with_taken_at: int
    # Already have an exif/user location — no estimation needed.
    with_real_location: int
    # Have an estimated location from a previous run.
    with_estimated_location: int
    # Eligible to (re-)estimate. = total_with_taken_at - with_real_location
    eligible: int


class TriggerEstimateIn(BaseModel):
    # Default 6 h. Caller can widen for a country-trip shoot or narrow
    # for densely-sampled walks.
    threshold_seconds: int = Field(
        default=21600, ge=60, le=7 * 24 * 60 * 60,
        description="Time window each anchor photo must fall within.",
    )


class TriggerEstimateOut(BaseModel):
    job_id: int
    root_id: int
    # How many photo_work rows the trigger newly pendinged. 0 means
    # every eligible photo already had the stage pending from a prior
    # click — caller can show "이미 큐에 있음" instead of "방금 등록".
    enqueued: int = 0
    eligible: int = 0


@router.get(
    "/{root_id}/locations/estimation-stats",
    response_model=LocationEstimateStats,
)
def location_estimation_stats(
    root_id: int, db: Session = Depends(get_db),
) -> LocationEstimateStats:
    """Counts for the admin progress card. Cheap — three SELECT COUNT(*).
    """
    from sqlalchemy import func
    from ..models import Photo, PhotoLocation

    root = db.get(Root, root_id)
    if root is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND)

    # Only count photos whose indexing pipeline is settled — matches
    # what location_estimator actually processes. Without this filter
    # the "eligible" counter includes in-flight photos the estimator
    # is going to skip anyway, making the bar misleading.
    total = db.execute(
        select(func.count(Photo.id)).where(
            Photo.root_id == root_id,
            Photo.taken_at.is_not(None),
            Photo.exif_status.in_(("ok", "partial")),
        )
    ).scalar() or 0
    real = db.execute(
        select(func.count(Photo.id))
        .join(PhotoLocation, PhotoLocation.photo_id == Photo.id)
        .where(
            Photo.root_id == root_id,
            Photo.taken_at.is_not(None),
            Photo.exif_status.in_(("ok", "partial")),
            (PhotoLocation.source.is_(None))
            | (PhotoLocation.source.in_(("exif", "user"))),
        )
    ).scalar() or 0
    est = db.execute(
        select(func.count(Photo.id))
        .join(PhotoLocation, PhotoLocation.photo_id == Photo.id)
        .where(
            Photo.root_id == root_id,
            Photo.taken_at.is_not(None),
            Photo.exif_status.in_(("ok", "partial")),
            PhotoLocation.source == "estimated",
        )
    ).scalar() or 0
    return LocationEstimateStats(
        total_with_taken_at=int(total),
        with_real_location=int(real),
        with_estimated_location=int(est),
        eligible=int(total - real),
    )


@router.post(
    "/{root_id}/estimate-locations",
    response_model=TriggerEstimateOut,
    status_code=status.HTTP_202_ACCEPTED,
)
def trigger_estimate_locations(
    root_id: int,
    body: TriggerEstimateIn,
    db: Session = Depends(get_db),
) -> TriggerEstimateOut:
    """Mark the estimate_location stage pending on every eligible
    photo in the root. Inline — scans rows and INSERTs/UPDATEs
    photo_work entries on the spot, returning when the queue is
    fully populated. Idempotent: enqueue_stage skips photos that
    already have the stage pending or ok, so a re-trigger costs
    only the SELECT.

    Used to fan out via the legacy `estimate_locations` job kind;
    that path is gone now that photo_work is the unit of work.
    """
    from sqlalchemy import select as _select
    from ..models import Photo, PhotoLocation
    from ..worker import photo_work as photo_work_mod

    root = db.get(Root, root_id)
    if root is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND)

    rows = db.execute(
        _select(Photo.id)
        .outerjoin(PhotoLocation, PhotoLocation.photo_id == Photo.id)
        .where(
            Photo.root_id == root_id,
            Photo.taken_at.is_not(None),
            Photo.exif_status.in_(("ok", "partial")),
            (PhotoLocation.photo_id.is_(None))
            | (PhotoLocation.source == "estimated"),
        )
    ).all()

    eligible = len(rows)
    enqueued = 0
    seen = 0
    for (pid,) in rows:
        if photo_work_mod.enqueue_stage(
            db,
            photo_id=int(pid),
            stage="estimate_location",
            priority=0,
            params={"threshold_seconds": int(body.threshold_seconds)},
        ):
            enqueued += 1
        seen += 1
        if seen % 500 == 0:
            db.commit()
    db.commit()
    # job_id is meaningless now (no jobs row); the UI uses enqueued /
    # eligible to give the user immediate feedback instead.
    return TriggerEstimateOut(
        job_id=0, root_id=root_id, enqueued=enqueued, eligible=eligible,
    )


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

"""Trash recovery — list, restore, and permanently delete trashed photos.

When a photo is deleted via the public API, its row is flipped to
`status='trashed'` and the original file is moved to
`data/trash/<photo_id>/<filename>` with a `_meta.json` sidecar
(see routes_photos._move_to_trash). This module gives admins a UI to
walk that directory, push files back to their original root, or
purge them for good.

Restore must be defensive: the original root may now be missing
(disk failure / root deleted), the source path may collide with a
re-imported file, or the trash dir may have been hand-edited. Each
restore attempt is reported individually so the UI can show
per-photo outcomes rather than blanket-failing the batch.
"""

from __future__ import annotations

import json
import logging
import shutil
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import delete as _delete
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import audit
from ..api.deps import get_db
from ..auth import require_admin, require_auth, require_can_delete
from ..models import (
    Photo,
    PhotoComment,
    PhotoFace,
    PhotoLocation,
    PhotoRating,
    PhotoTag,
    Root,
    User,
)
from ..paths import TRASH_DIR
from ..scanner.utils import join_root

log = logging.getLogger(__name__)

router = APIRouter(prefix="/admin/trash", tags=["admin", "trash"])


class TrashItem(BaseModel):
    """One row in the trash listing.

    `trash_present` distinguishes "row says trashed but no file in
    data/trash/<id>/" — usually means someone wiped the dir by hand.
    Restore is still possible if the DB row + root + rel_path are
    intact, but the file content is lost.
    """

    id: int
    filename: str
    rel_path: str
    root_id: int
    root_label: str | None
    sha256: str | None
    file_size: int | None
    media_kind: str
    taken_at: datetime | None
    deleted_at: datetime | None
    deleted_by: str | None
    trash_present: bool


class TrashPage(BaseModel):
    total: int
    page: int
    page_size: int
    items: list[TrashItem]
    # Aggregate trash footprint + free space on the volume that holds
    # data/trash, so the admin UI can show "휴지통 사용: X GB · 여유: Y GB"
    # at a glance. trash_bytes is summed from photos.file_size (the
    # actual byte counts of files inside data/trash are usually equal,
    # and reading them all would be expensive on a big trash).
    trash_bytes: int = 0
    disk_free_bytes: int = 0
    disk_total_bytes: int = 0


def _read_trash_meta(photo_id: int) -> dict:
    """Best-effort read of the _meta.json sidecar written at trash time."""
    meta_path = TRASH_DIR / str(photo_id) / "_meta.json"
    if not meta_path.exists():
        return {}
    try:
        return json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        log.warning("trash meta read failed for %s: %s", photo_id, e)
        return {}


def _trash_file_path(photo_id: int, filename: str) -> Path | None:
    """Locate the actual moved file inside data/trash/<id>/.

    Sidecar may name a timestamp-prefixed variant if the same id was
    deleted twice — falls back to scanning the dir for the first non-meta
    file.
    """
    d = TRASH_DIR / str(photo_id)
    if not d.exists():
        return None
    # Try the meta-recorded name first.
    meta = _read_trash_meta(photo_id)
    if meta.get("trash_path"):
        # `trash_path` is stored relative to TRASH_DIR.
        cand = TRASH_DIR / meta["trash_path"]
        if cand.exists():
            return cand
    # Direct filename.
    cand = d / filename
    if cand.exists():
        return cand
    # Last resort — first file that isn't the meta sidecar.
    for f in sorted(d.iterdir()):
        if f.name == "_meta.json" or f.is_dir():
            continue
        return f
    return None


class IndexBucket(BaseModel):
    # Plain numeric label like "1", "2" (= "0–999th item", "1000–1999th",
    # …). Trash is sorted by Photo.id DESC and deleted_at lives in the
    # JSON sidecar, so there's no monotonic time axis to anchor a real
    # year/month histogram to — the minimap just needs scrub markers.
    label: str
    count: int


@router.get("/index-histogram", response_model=list[IndexBucket])
def index_histogram(
    all: bool = False,
    user: User = Depends(require_auth),
    db: Session = Depends(get_db),
) -> list[IndexBucket]:
    """Bucketed item count for the trash list minimap. Same filters
    that /trash applies (own-only vs. all), bucket size = 1000 items.
    Returned in the same Photo.id DESC order the list uses.
    """
    base = select(func.count()).select_from(
        select(Photo).where(Photo.status == "trashed").subquery()
    )
    q = select(Photo.id).where(Photo.status == "trashed")
    if not (user.is_admin and all):
        q = q.where(Photo.trashed_by_user_id == user.id)
        base = select(func.count()).select_from(
            select(Photo)
            .where(Photo.status == "trashed",
                   Photo.trashed_by_user_id == user.id)
            .subquery()
        )
    total = int(db.execute(base).scalar_one() or 0)
    if total <= 0:
        return []
    BUCKET = 1000
    out: list[IndexBucket] = []
    remaining = total
    i = 0
    while remaining > 0:
        n = min(BUCKET, remaining)
        # Label is the bucket's starting count rounded down to the
        # thousand — "1k", "2k", … — readable at a glance on the rail.
        label = f"{(i * BUCKET) // 1000 + 1}k" if i > 0 else "최신"
        out.append(IndexBucket(label=label, count=n))
        remaining -= n
        i += 1
    return out


@router.get("", response_model=TrashPage)
def list_trash(
    page: int = 1,
    page_size: int = 60,
    all: bool = False,
    user: User = Depends(require_auth),
    db: Session = Depends(get_db),
) -> TrashPage:
    """Photos currently marked `trashed`. By default this is scoped
    to deletions the caller themselves performed (P5 isolation); admin
    callers can pass `?all=true` to see everything (including legacy
    rows where trashed_by_user_id is NULL).

    `deleted_at` is read from the sidecar — falling back to None when the
    sidecar is missing (e.g. very old rows from before the meta-writer
    landed). Sort key is row id desc as a stable secondary so the list
    has a consistent order even when sidecars are absent.
    """
    page = max(1, page)
    page_size = max(1, min(page_size, 500))

    base = select(Photo).where(Photo.status == "trashed")
    # Non-admin → only your own deletions. Admin → all by default, but
    # ?all=false lets them see just what they deleted (useful for
    # "show me what I did").
    if not (user.is_admin and all):
        base = base.where(Photo.trashed_by_user_id == user.id)
    total = db.execute(select(func.count()).select_from(base.subquery())).scalar_one()

    rows = db.execute(
        base.order_by(Photo.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    ).scalars().all()

    roots = {r.id: r for r in db.execute(select(Root)).scalars().all()}

    # Resolve trashed_by_user_id → username in one round-trip so the
    # admin "전체" view can show 삭제자 without N user lookups.
    deleter_user_ids = {
        p.trashed_by_user_id for p in rows
        if p.trashed_by_user_id is not None
    }
    deleter_names: dict[int, str] = {}
    if deleter_user_ids:
        for u in db.execute(
            select(User.id, User.username).where(User.id.in_(deleter_user_ids))
        ).all():
            deleter_names[int(u[0])] = str(u[1])

    items: list[TrashItem] = []
    for p in rows:
        meta = _read_trash_meta(p.id)
        deleted_at = None
        if meta.get("deleted_at"):
            try:
                deleted_at = datetime.fromisoformat(meta["deleted_at"].rstrip("Z"))
            except ValueError:
                pass
        # Prefer the live DB → users lookup (renames stay correct);
        # fall back to the sidecar snapshot for legacy rows where
        # trashed_by_user_id is NULL.
        deleted_by: str | None
        if p.trashed_by_user_id is not None:
            deleted_by = deleter_names.get(int(p.trashed_by_user_id)) \
                or meta.get("deleted_by") \
                or f"#{p.trashed_by_user_id}"
        else:
            deleted_by = meta.get("deleted_by")
        root = roots.get(p.root_id)
        items.append(
            TrashItem(
                id=p.id,
                filename=p.filename,
                rel_path=p.rel_path,
                root_id=p.root_id,
                root_label=root.label if root else None,
                sha256=p.sha256,
                file_size=p.file_size,
                media_kind=p.media_kind,
                taken_at=p.taken_at,
                deleted_at=deleted_at,
                deleted_by=deleted_by,
                trash_present=_trash_file_path(p.id, p.filename) is not None,
            )
        )
    # Trash footprint + disk free. file_size is nullable so coalesce
    # to 0; shutil.disk_usage is dirt-cheap (one statvfs).
    import shutil
    trash_bytes = db.execute(
        select(func.coalesce(func.sum(Photo.file_size), 0))
        .where(Photo.status == "trashed")
    ).scalar_one() or 0
    disk_free = disk_total = 0
    try:
        usage = shutil.disk_usage(TRASH_DIR)
        disk_free = usage.free
        disk_total = usage.total
    except OSError:
        pass
    return TrashPage(
        total=total,
        page=page,
        page_size=page_size,
        items=items,
        trash_bytes=int(trash_bytes),
        disk_free_bytes=int(disk_free),
        disk_total_bytes=int(disk_total),
    )


class TrashIdsIn(BaseModel):
    photo_ids: list[int]


class RestoreOutcome(BaseModel):
    photo_id: int
    ok: bool
    reason: str | None = None


class RestoreResponse(BaseModel):
    restored: int
    failed: int
    results: list[RestoreOutcome]


def _restore_one(p: Photo, root: Root) -> RestoreOutcome:
    """Try to move data/trash/<id>/<filename> back to its original root.

    Refuses to overwrite an existing file at the destination — instead
    surfaces it as a per-photo failure so the admin can decide (e.g. the
    user re-imported the same file after deletion).
    """
    src = _trash_file_path(p.id, p.filename)
    if src is None:
        return RestoreOutcome(
            photo_id=p.id, ok=False, reason="휴지통에 원본 파일이 없습니다"
        )
    dest = Path(join_root(root.abs_path, p.rel_path))
    if dest.exists():
        return RestoreOutcome(
            photo_id=p.id,
            ok=False,
            reason=f"원본 위치에 이미 같은 파일이 있습니다: {p.rel_path}",
        )
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dest))
    except (OSError, shutil.Error) as e:
        return RestoreOutcome(
            photo_id=p.id, ok=False, reason=f"파일 이동 실패: {e}"
        )

    # Clean up the trash dir if empty (meta sidecar is fine to leave —
    # it'll be ignored on future list calls). Try to remove the dir
    # entirely; ignore failures (something else may be in there).
    try:
        trash_dir = TRASH_DIR / str(p.id)
        if trash_dir.exists():
            meta = trash_dir / "_meta.json"
            if meta.exists():
                meta.unlink()
            trash_dir.rmdir()
    except OSError:
        pass

    p.status = "active"
    return RestoreOutcome(photo_id=p.id, ok=True)


@router.post("/restore", response_model=RestoreResponse)
def restore(
    body: TrashIdsIn,
    user: User = Depends(require_can_delete),
    db: Session = Depends(get_db),
) -> RestoreResponse:
    """Restore selected trashed photos by moving the file back and
    flipping status to 'active'. Non-admin can only restore photos
    they themselves trashed; admin can restore anyone's.
    """
    if not body.photo_ids:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "사진을 선택하세요")
    if len(body.photo_ids) > 1000:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, "한 번에 1000장까지 가능합니다"
        )

    q = select(Photo).where(
        Photo.id.in_(body.photo_ids), Photo.status == "trashed",
    )
    if not user.is_admin:
        q = q.where(Photo.trashed_by_user_id == user.id)
    rows = db.execute(q).scalars().all()
    roots = {r.id: r for r in db.execute(select(Root)).scalars().all()}

    results: list[RestoreOutcome] = []
    for p in rows:
        root = roots.get(p.root_id)
        if root is None:
            results.append(
                RestoreOutcome(
                    photo_id=p.id,
                    ok=False,
                    reason="원본 루트가 더 이상 존재하지 않습니다",
                )
            )
            continue
        outcome = _restore_one(p, root)
        results.append(outcome)
        if outcome.ok:
            # Clear trashed_by so a future re-deletion captures the new
            # actor (could be a different family member).
            p.trashed_by_user_id = None
            audit.record(
                db, user, "photo.restore", "photo", p.id,
                detail={"filename": p.filename},
            )
    db.commit()

    restored = sum(1 for r in results if r.ok)
    return RestoreResponse(
        restored=restored,
        failed=len(results) - restored,
        results=results,
    )


class PurgeResponse(BaseModel):
    purged: int
    failed: int


def _purge_one(db: Session, p: Photo) -> bool:
    """Permanently delete trash dir + all DB rows referencing this photo."""
    trash_dir = TRASH_DIR / str(p.id)
    try:
        if trash_dir.exists():
            shutil.rmtree(trash_dir)
    except OSError as e:
        log.warning("trash rmtree failed for %s: %s", p.id, e)
        return False

    # Clean up everything that references the photo before deleting the
    # row itself. Some of these tables may not exist on older DBs — the
    # individual deletes are tolerant.
    for stmt in (
        _delete(PhotoLocation).where(PhotoLocation.photo_id == p.id),
        _delete(PhotoRating).where(PhotoRating.photo_id == p.id),
        _delete(PhotoComment).where(PhotoComment.photo_id == p.id),
        _delete(PhotoTag).where(PhotoTag.photo_id == p.id),
        _delete(PhotoFace).where(PhotoFace.photo_id == p.id),
    ):
        try:
            db.execute(stmt)
        except Exception:
            db.rollback()
    from .. import fts as _fts
    _fts.delete_photo(db, p.id)
    db.delete(p)
    return True


@router.post("/delete-permanently", response_model=PurgeResponse)
def delete_permanently(
    body: TrashIdsIn,
    user: User = Depends(require_can_delete),
    db: Session = Depends(get_db),
) -> PurgeResponse:
    """Permanently delete selected trashed photos — file + DB row + all
    related rows (comments / ratings / tags / faces / locations).
    Non-admin can only purge photos they themselves trashed; admin
    can purge anyone's.
    """
    if not body.photo_ids:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "사진을 선택하세요")
    if len(body.photo_ids) > 1000:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, "한 번에 1000장까지 가능합니다"
        )

    q = select(Photo).where(
        Photo.id.in_(body.photo_ids), Photo.status == "trashed",
    )
    if not user.is_admin:
        q = q.where(Photo.trashed_by_user_id == user.id)
    rows = db.execute(q).scalars().all()

    purged = 0
    failed = 0
    for p in rows:
        # Capture before _purge_one mutates the row to nothing.
        pid, fname = p.id, p.filename
        if _purge_one(db, p):
            purged += 1
            audit.record(
                db, user, "photo.purge", "photo", pid,
                detail={"filename": fname},
            )
        else:
            failed += 1
    db.commit()
    return PurgeResponse(purged=purged, failed=failed)


@router.post("/empty", response_model=PurgeResponse)
def empty_trash(
    user: User = Depends(require_admin), db: Session = Depends(get_db)
) -> PurgeResponse:
    """Permanently delete every photo currently in the trash.

    Equivalent to selecting all + delete-permanently, but doesn't require
    the UI to enumerate ids client-side first.
    """
    rows = db.execute(
        select(Photo).where(Photo.status == "trashed")
    ).scalars().all()
    purged = 0
    failed = 0
    for p in rows:
        pid, fname = p.id, p.filename
        if _purge_one(db, p):
            purged += 1
            audit.record(
                db, user, "photo.purge", "photo", pid,
                detail={"filename": fname, "via": "empty"},
            )
        else:
            failed += 1
    db.commit()
    return PurgeResponse(purged=purged, failed=failed)

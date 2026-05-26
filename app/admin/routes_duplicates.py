"""Admin endpoints for duplicate-file management.

Photos with the same sha256 but different (root, rel_path) are "duplicates"
in catalog terms — they point at byte-identical files stored in more than
one location. The thumbnail cache already dedupes (paths are keyed on
sha256), so eliminating duplicate rows is a catalog-tidiness gain rather
than disk-reclaim, unless the user also lets the worker actually trash
the underlying file (which it does — moves to data/trash/).
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import desc, func, select
from sqlalchemy.orm import Session

from ..api.deps import get_db
from ..auth import require_admin
from ..models import Photo, Root, User

router = APIRouter(prefix="/admin/duplicates", tags=["admin", "duplicates"])


class DupStats(BaseModel):
    groups: int           # number of sha256s with more than one photo
    duplicate_rows: int   # sum of (count - 1) across groups
    wasted_bytes: int     # sum of (count - 1) * file_size


class DupMember(BaseModel):
    id: int
    root_id: int
    root_label: str
    # Exposed so the admin UI can disable the per-group trash button
    # when every member sits on a readonly root (the bulk-delete would
    # come back as all skipped_readonly anyway).
    root_readonly: bool
    rel_path: str
    filename: str
    taken_at: datetime | None = None


class DupGroup(BaseModel):
    sha256: str
    count: int
    file_size: int | None = None
    photos: list[DupMember]


class DupGroupPage(BaseModel):
    total_groups: int
    page: int
    page_size: int
    items: list[DupGroup]


def _dup_subquery():
    """Subquery: one row per sha256 that has 2+ active photos."""
    return (
        select(
            Photo.sha256.label("sha256"),
            func.count(Photo.id).label("n"),
            func.max(Photo.file_size).label("file_size"),
        )
        .where(Photo.sha256.is_not(None), Photo.status == "active")
        .group_by(Photo.sha256)
        .having(func.count(Photo.id) > 1)
    )


@router.get("/stats", response_model=DupStats)
def stats(
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
) -> DupStats:
    subq = _dup_subquery().subquery()
    row = db.execute(
        select(
            func.count(subq.c.sha256),
            func.coalesce(func.sum(subq.c.n - 1), 0),
            func.coalesce(func.sum((subq.c.n - 1) * subq.c.file_size), 0),
        )
    ).one()
    return DupStats(
        groups=int(row[0] or 0),
        duplicate_rows=int(row[1] or 0),
        wasted_bytes=int(row[2] or 0),
    )


@router.get("/groups", response_model=DupGroupPage)
def list_groups(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
) -> DupGroupPage:
    base = _dup_subquery()
    total = db.execute(
        select(func.count()).select_from(base.subquery())
    ).scalar_one()
    if total == 0:
        return DupGroupPage(total_groups=0, page=page, page_size=page_size, items=[])

    page_q = (
        base.order_by(desc("n"), Photo.sha256)
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    rows = db.execute(page_q).all()
    if not rows:
        return DupGroupPage(total_groups=total, page=page, page_size=page_size, items=[])

    shas = [r[0] for r in rows]
    # Sort so the "first" item per group (the suggested keep) is the
    # OLDEST instance of the file — usually the original, before
    # someone copied it into a second folder. Falls back to mtime
    # then to alphabetical so the order stays deterministic even
    # when taken_at / mtime are missing on every row.
    members_rows = db.execute(
        select(
            Photo.id, Photo.sha256, Photo.root_id, Root.label, Root.readonly,
            Photo.rel_path, Photo.filename, Photo.taken_at, Photo.mtime,
        )
        .join(Root, Root.id == Photo.root_id)
        .where(Photo.sha256.in_(shas), Photo.status == "active")
        .order_by(
            Photo.sha256,
            Photo.taken_at.asc().nullslast(),
            Photo.mtime.asc().nullslast(),
            Root.label,
            Photo.rel_path,
        )
    ).all()
    by_sha: dict[str, list[DupMember]] = {}
    for r in members_rows:
        by_sha.setdefault(r[1], []).append(
            DupMember(
                id=r[0], root_id=r[2], root_label=r[3], root_readonly=bool(r[4]),
                rel_path=r[5], filename=r[6], taken_at=r[7],
            )
        )
    # r[8] (mtime) is fetched only to drive the ORDER BY above; the
    # response payload doesn't need it.

    items = [
        DupGroup(
            sha256=r[0], count=int(r[1]), file_size=r[2],
            photos=by_sha.get(r[0], []),
        )
        for r in rows
    ]
    return DupGroupPage(total_groups=total, page=page, page_size=page_size, items=items)

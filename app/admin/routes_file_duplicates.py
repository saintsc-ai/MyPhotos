"""Admin endpoints for duplicate *file* management (kind='file' roots).

Sibling of routes_duplicates.py (which handles photos). Files sharing a
sha256 across different (root, rel_path) locations are byte-identical
duplicates. Unlike photos — which are soft-trashed (recoverable) — files
have no trash: deletion is a permanent os.remove on writable roots only
(see /api/files/delete). So this module is detection + display ONLY; the
actual removal is driven manually from the explorer's file-delete endpoint
after the admin picks which copies to keep. No auto-cleanup worker here on
purpose — a one-click bulk permanent-delete of user files is too dangerous.
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import String as sa_String
from sqlalchemy import asc, desc, func, literal, select
from sqlalchemy.orm import Session

from ..api.deps import get_db
from ..auth import require_admin
from ..models import File, Root, User

router = APIRouter(prefix="/admin/file-duplicates", tags=["admin", "duplicates"])


class FileDupStats(BaseModel):
    groups: int           # number of sha256s with more than one file
    duplicate_rows: int   # sum of (count - 1) across groups
    wasted_bytes: int     # sum of (count - 1) * file_size


class FileDupMember(BaseModel):
    id: int
    root_id: int
    root_label: str
    # Lets the UI disable the delete affordance for copies on readonly
    # roots (the file-delete endpoint refuses them anyway).
    root_readonly: bool
    rel_path: str
    filename: str
    ext: str | None = None
    mime: str | None = None
    file_size: int | None = None
    mtime: datetime | None = None


class FileDupGroup(BaseModel):
    sha256: str
    count: int
    file_size: int | None = None
    files: list[FileDupMember]


class FileDupGroupPage(BaseModel):
    total_groups: int
    page: int
    page_size: int
    items: list[FileDupGroup]


def _dup_subquery():
    """One row per sha256 that has 2+ active files.

    No thumbnail gate (files have none) — just active + hashed. `dir_variants`
    counts distinct (root_id, parent-folder) tuples so same-folder dup groups
    (accidental re-copies) can be surfaced first, mirroring the photo view.
    """
    parent = func.substr(
        File.rel_path, 1, func.length(File.rel_path) - func.length(File.filename),
    )
    dir_key = func.cast(File.root_id, sa_String) + literal("|") + parent
    return (
        select(
            File.sha256.label("sha256"),
            func.count(File.id).label("n"),
            func.max(File.file_size).label("file_size"),
            func.count(func.distinct(dir_key)).label("dir_variants"),
            func.max(File.mtime).label("max_mtime"),
        )
        .where(
            File.sha256.is_not(None),
            File.status == "active",
        )
        .group_by(File.sha256)
        .having(func.count(File.id) > 1)
    )


@router.get("/stats", response_model=FileDupStats)
def stats(
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
) -> FileDupStats:
    subq = _dup_subquery().subquery()
    row = db.execute(
        select(
            func.count(subq.c.sha256),
            func.coalesce(func.sum(subq.c.n - 1), 0),
            func.coalesce(func.sum((subq.c.n - 1) * subq.c.file_size), 0),
        )
    ).one()
    return FileDupStats(
        groups=int(row[0] or 0),
        duplicate_rows=int(row[1] or 0),
        wasted_bytes=int(row[2] or 0),
    )


@router.get("/groups", response_model=FileDupGroupPage)
def list_groups(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
) -> FileDupGroupPage:
    base = _dup_subquery()
    total = db.execute(
        select(func.count()).select_from(base.subquery())
    ).scalar_one()
    if total == 0:
        return FileDupGroupPage(total_groups=0, page=page, page_size=page_size, items=[])

    # Sort: most-recently-modified group first, then same-folder groups
    # (dir_variants ASC), then larger files, then sha for stable paging.
    from sqlalchemy import column as sa_column
    page_q = (
        base.order_by(
            sa_column("max_mtime").desc().nullslast(),
            asc("dir_variants"),
            desc("file_size"),
            File.sha256,
        )
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    rows = db.execute(page_q).all()
    if not rows:
        return FileDupGroupPage(total_groups=total, page=page, page_size=page_size, items=[])

    shas = [r[0] for r in rows]
    # Oldest copy first per group (the suggested keep) — usually the
    # original before it was re-copied elsewhere.
    members_rows = db.execute(
        select(
            File.id, File.sha256, File.root_id, Root.label, Root.readonly,
            File.rel_path, File.filename, File.ext, File.mime,
            File.file_size, File.mtime,
        )
        .join(Root, Root.id == File.root_id)
        .where(
            File.sha256.in_(shas),
            File.status == "active",
        )
        .order_by(
            File.sha256,
            File.mtime.asc().nullslast(),
            Root.label,
            File.rel_path,
        )
    ).all()
    by_sha: dict[str, list[FileDupMember]] = {}
    for r in members_rows:
        by_sha.setdefault(r[1], []).append(
            FileDupMember(
                id=r[0], root_id=r[2], root_label=r[3], root_readonly=bool(r[4]),
                rel_path=r[5], filename=r[6], ext=r[7], mime=r[8],
                file_size=r[9], mtime=r[10],
            )
        )

    items = [
        FileDupGroup(
            sha256=r[0], count=int(r[1]), file_size=r[2],
            files=by_sha.get(r[0], []),
        )
        for r in rows
    ]
    return FileDupGroupPage(total_groups=total, page=page, page_size=page_size, items=items)

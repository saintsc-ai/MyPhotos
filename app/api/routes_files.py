"""Read API for the files (document) domain — the explorer backbone.

Serves the folder tree / listing, file metadata, original-file download, and
search for ``kind='file'`` roots. Photo endpoints are unaffected (separate
router, separate tables). Write operations (upload / new folder / rename /
move / delete — the "full explorer") land in a follow-up.

Access control reuses the folder-level ACL: a file's access level is the
level of its containing folder (effective_folder_level), so root_acl /
folder_acl grants apply to documents exactly as they do to photos.
"""

from __future__ import annotations

import os
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import FileResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import fts
from ..auth import require_auth
from ..auth_acl import effective_folder_level, effective_root_level
from ..models import File, Root, User
from ..scanner.utils import join_root
from .deps import get_db

router = APIRouter(prefix="/files", tags=["files"])

SEARCH_LIMIT = 300


def _parent_dir(rel_path: str) -> str:
    """Folder portion of a POSIX rel_path ('' for a top-level file)."""
    return rel_path.rsplit("/", 1)[0] if "/" in rel_path else ""


def _file_root_or_404(db: Session, root_id: int) -> Root:
    root = db.get(Root, root_id)
    if root is None or getattr(root, "kind", "photo") != "file":
        raise HTTPException(status.HTTP_404_NOT_FOUND, "file root not found")
    return root


def _norm_folder(path: Optional[str]) -> str:
    """Normalize a folder path arg: forward slashes, no leading/trailing '/'."""
    return (path or "").strip().replace("\\", "/").strip("/")


def list_folder(
    db: Session, root_id: int, folder: str,
) -> tuple[list[str], list[File]]:
    """Return (immediate subfolder names, direct child File rows) for
    `folder` under `root_id`. Pure DB derivation from stored rel_paths —
    the files domain has no separate directory table. Testable without HTTP.
    """
    prefix = (folder + "/") if folder else ""
    rows = db.execute(
        select(File).where(
            File.root_id == root_id,
            File.status == "active",
            File.rel_path.like(prefix.replace("%", r"\%").replace("_", r"\_") + "%", escape="\\")
            if prefix else File.rel_path.like("%"),
        )
    ).scalars().all()
    subfolders: set[str] = set()
    files: list[File] = []
    plen = len(prefix)
    for f in rows:
        tail = f.rel_path[plen:]
        if "/" in tail:
            subfolders.add(tail.split("/", 1)[0])
        elif tail:  # direct child file (not the folder marker itself)
            files.append(f)
    files.sort(key=lambda x: x.filename.lower())
    return sorted(subfolders, key=str.lower), files


def _file_out(f: File) -> dict:
    return {
        "id": f.id,
        "root_id": f.root_id,
        "rel_path": f.rel_path,
        "filename": f.filename,
        "ext": f.ext,
        "mime": f.mime,
        "size": f.file_size,
        "mtime": f.mtime.isoformat() if f.mtime else None,
        "text_status": f.text_status,
    }


@router.get("/roots")
def list_file_roots(
    db: Session = Depends(get_db), user: User = Depends(require_auth),
):
    """File-kind roots the user can see — powers the mode switch + tree root."""
    roots = db.execute(
        select(Root).where(Root.kind == "file", Root.enabled.is_(True))
        .order_by(Root.label)
    ).scalars().all()
    out = []
    for r in roots:
        if effective_root_level(db, user, r.id) == "hidden":
            continue
        out.append({"id": r.id, "label": r.label, "readonly": r.readonly})
    return out


@router.get("/list")
def list_files(
    root_id: int = Query(...),
    path: str = Query(""),
    db: Session = Depends(get_db),
    user: User = Depends(require_auth),
):
    """List immediate subfolders + files in a folder of a file root."""
    _file_root_or_404(db, root_id)
    folder = _norm_folder(path)
    if effective_folder_level(db, user, root_id, folder) == "hidden":
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    subfolders, files = list_folder(db, root_id, folder)
    return {
        "root_id": root_id,
        "path": folder,
        "folders": subfolders,
        "files": [_file_out(f) for f in files],
    }


@router.get("/search")
def search_files(
    q: str = Query(..., min_length=1),
    root_id: Optional[int] = Query(None),
    db: Session = Depends(get_db),
    user: User = Depends(require_auth),
):
    """Search files by name/path (content later). Trigram FTS when the query
    is long enough, else a LIKE fallback. ACL-filtered per containing folder.
    """
    # Which file roots may we search?
    root_ids: list[int]
    if root_id is not None:
        _file_root_or_404(db, root_id)
        if effective_root_level(db, user, root_id) == "hidden":
            raise HTTPException(status.HTTP_404_NOT_FOUND)
        root_ids = [root_id]
    else:
        root_ids = [
            r.id for r in db.execute(
                select(Root).where(Root.kind == "file", Root.enabled.is_(True))
            ).scalars().all()
            if effective_root_level(db, user, r.id) != "hidden"
        ]
    if not root_ids:
        return {"query": q, "results": []}

    match = fts.build_match_query(q) if fts.is_file_fts_available(db) else None
    base = select(File).where(File.root_id.in_(root_ids), File.status == "active")
    if match is not None:
        from sqlalchemy import text as _text
        ids = [r[0] for r in db.execute(
            _text("SELECT rowid FROM file_fts WHERE file_fts MATCH :m LIMIT :lim"),
            {"m": match, "lim": SEARCH_LIMIT * 2},
        ).all()]
        if not ids:
            return {"query": q, "results": []}
        base = base.where(File.id.in_(ids))
    else:
        like = "%" + q.replace("%", r"\%").replace("_", r"\_") + "%"
        base = base.where(File.rel_path.like(like, escape="\\"))
    rows = db.execute(base.limit(SEARCH_LIMIT * 2)).scalars().all()

    out = []
    for f in rows:
        if effective_folder_level(db, user, f.root_id, _parent_dir(f.rel_path)) == "hidden":
            continue
        out.append(_file_out(f))
        if len(out) >= SEARCH_LIMIT:
            break
    return {"query": q, "results": out}


def _require_file_read(db: Session, user: User, f: File) -> None:
    if effective_folder_level(db, user, f.root_id, _parent_dir(f.rel_path)) == "hidden":
        raise HTTPException(status.HTTP_404_NOT_FOUND)


@router.get("/{file_id}")
def get_file(
    file_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(require_auth),
):
    f = db.get(File, file_id)
    if f is None or f.status != "active":
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    _require_file_read(db, user, f)
    return _file_out(f)


@router.get("/{file_id}/download")
def download_file(
    file_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(require_auth),
):
    f = db.get(File, file_id)
    if f is None or f.status != "active":
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    _require_file_read(db, user, f)
    root = db.get(Root, f.root_id)
    if root is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    abs_path = join_root(root.abs_path, f.rel_path)
    if not os.path.exists(abs_path):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "file missing on disk")
    return FileResponse(
        abs_path,
        filename=f.filename,
        media_type=f.mime or "application/octet-stream",
    )

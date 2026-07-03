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
import shutil
from pathlib import Path
from typing import Optional

from fastapi import (
    APIRouter, Body, Depends, File as UploadFileParam, Form, HTTPException,
    Query, UploadFile, status,
)
from fastapi.responses import FileResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import fts
from ..auth import require_auth, require_can_delete, require_can_upload
from ..auth_acl import effective_folder_level, effective_root_level
from ..models import File, FileText, Root, User
from ..scanner.utils import join_root, nfc
from .deps import get_db

router = APIRouter(prefix="/files", tags=["files"])

SEARCH_LIMIT = 300
LIST_FILE_LIMIT = 3000  # cap direct-child files per folder listing


def _parent_dir(rel_path: str) -> str:
    """Folder portion of a POSIX rel_path ('' for a top-level file)."""
    return rel_path.rsplit("/", 1)[0] if "/" in rel_path else ""


def _file_root_or_404(db: Session, root_id: int) -> Root:
    root = db.get(Root, root_id)
    if root is None or getattr(root, "kind", "photo") not in ("file", "both"):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "file root not found")
    return root


def _norm_folder(path: Optional[str]) -> str:
    """Normalize a folder path arg: forward slashes, no leading/trailing '/'."""
    return (path or "").strip().replace("\\", "/").strip("/")


def list_folder(
    db: Session, root_id: int, folder: str,
) -> tuple[list[str], list[File], set[str]]:
    """Return (immediate subfolder names, direct child File rows, names of
    those subfolders that themselves contain deeper subfolders) for `folder`
    under `root_id`. The third element lets the tree UI omit an expand toggle
    on leaf folders. Pure DB derivation from stored rel_paths — the files
    domain has no separate directory table. Testable without HTTP.
    """
    # Direct child files — indexed equality on (root_id, parent). No subtree
    # scan: we only ever touch this folder's own files.
    files = db.execute(
        select(File).where(
            File.root_id == root_id,
            File.status == "active",
            File.parent == folder,
        ).order_by(File.filename).limit(LIST_FILE_LIMIT)
    ).scalars().all()
    # Immediate subfolders: distinct folder paths that contain files
    # (index-only DISTINCT on (root_id, parent), bounded by directory count,
    # not file count), then derive the segment immediately under `folder`.
    dirs = db.execute(
        select(File.parent).where(
            File.root_id == root_id, File.status == "active",
        ).distinct()
    ).scalars().all()
    base = folder + "/" if folder else ""
    subs: set[str] = set()
    with_children: set[str] = set()
    for p in dirs:
        if not p:
            continue
        if folder:
            if p == folder or not p.startswith(base):
                continue
            rest = p[len(base):]
        else:
            rest = p
        seg, _, deeper = rest.partition("/")
        if seg:
            subs.add(seg)
            if deeper:  # a dir path reaches below this subfolder → it has children
                with_children.add(seg)
    return sorted(subs, key=str.lower), files, with_children


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
        select(Root).where(Root.kind.in_(("file", "both")), Root.enabled.is_(True))
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
    subfolders, files, with_children = list_folder(db, root_id, folder)
    return {
        "root_id": root_id,
        "path": folder,
        "folders": subfolders,
        "folders_with_children": sorted(with_children, key=str.lower),
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
                select(Root).where(Root.kind.in_(("file", "both")), Root.enabled.is_(True))
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


# ---------------------------------------------------------------------------
# write ops — writable (readonly=False) file roots only + can_upload/can_delete
# ---------------------------------------------------------------------------
_ILLEGAL = set('/\\:*?"<>|')


def _writable_file_root(db: Session, root_id: int) -> Root:
    root = _file_root_or_404(db, root_id)
    if root.readonly:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"'{root.label}'는 읽기 전용입니다 — 관리에서 RO 토글을 끄세요")
    return root


def _safe_abs(root_abs: str, rel: str) -> Path:
    base = Path(root_abs).resolve()
    cand = (base / rel).resolve()
    try:
        cand.relative_to(base)
    except ValueError:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "경로가 root 밖을 가리킵니다")
    return cand


def _safe_name(raw: str) -> str:
    n = nfc((raw or "").strip())
    if not n or n in (".", ".."):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "잘못된 이름")
    if any(c in _ILLEGAL for c in n):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "이름에 쓸 수 없는 문자")
    return n


def _enqueue_index(db: Session, file_id: int) -> None:
    from ..worker.jobs import enqueue
    from ..worker import photo_work as pw
    enqueue(db, kind="index_file_generic",
            payload={"file_id": file_id}, priority=pw.PRIO_NEW_INDEX)


@router.post("/upload")
async def upload_files(
    root_id: int = Form(...),
    path: str = Form(""),
    files: list[UploadFile] = UploadFileParam(...),
    db: Session = Depends(get_db),
    user: User = Depends(require_can_upload),
):
    root = _writable_file_root(db, root_id)
    folder = _norm_folder(path)
    if effective_folder_level(db, user, root_id, folder) == "hidden":
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    dest_dir = _safe_abs(root.abs_path, folder)
    os.makedirs(dest_dir, exist_ok=True)
    out = []
    for uf in files:
        name = _safe_name(os.path.basename(uf.filename or "file"))
        rel = (folder + "/" + name) if folder else name
        dest = _safe_abs(root.abs_path, rel)
        with open(dest, "wb") as w:
            shutil.copyfileobj(uf.file, w)
        st = os.stat(dest)
        ext = name.rsplit(".", 1)[1].lower() if "." in name else ""
        import mimetypes
        from datetime import datetime as _dt
        row = db.execute(
            select(File).where(File.root_id == root.id, File.rel_path == rel)
        ).scalar_one_or_none()
        if row is None:
            row = File(root_id=root.id, rel_path=rel, filename=name, ext=ext,
                       parent=folder,
                       mime=mimetypes.guess_type(name)[0], file_size=st.st_size,
                       mtime=_dt.fromtimestamp(st.st_mtime),
                       content_signature=f"{st.st_size}:{st.st_mtime_ns}",
                       status="active", text_status="pending",
                       owner_user_id=user.id)
            db.add(row)
        else:
            row.file_size = st.st_size
            row.mtime = _dt.fromtimestamp(st.st_mtime)
            row.content_signature = f"{st.st_size}:{st.st_mtime_ns}"
            row.status = "active"
            row.sha256 = None
            row.text_status = "pending"
        db.flush()
        _enqueue_index(db, row.id)
        out.append({"id": row.id, "rel_path": rel})
    db.commit()
    fts.bulk_rebuild_files(db, [o["id"] for o in out])
    db.commit()
    return {"uploaded": len(out), "files": out}


class RenameFileIn(BaseModel):
    new_name: str


@router.post("/{file_id}/rename")
def rename_file(
    file_id: int,
    payload: RenameFileIn,
    db: Session = Depends(get_db),
    user: User = Depends(require_can_upload),
):
    f = db.get(File, file_id)
    if f is None or f.status != "active":
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    root = _writable_file_root(db, f.root_id)
    parent = _parent_dir(f.rel_path)
    if effective_folder_level(db, user, f.root_id, parent) == "hidden":
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    new_name = _safe_name(payload.new_name)
    new_rel = (parent + "/" + new_name) if parent else new_name
    if new_rel == f.rel_path:
        return _file_out(f)
    dup = db.execute(
        select(File.id).where(File.root_id == f.root_id, File.rel_path == new_rel)
    ).scalar_one_or_none()
    dst = _safe_abs(root.abs_path, new_rel)
    if dup is not None or dst.exists():
        raise HTTPException(status.HTTP_409_CONFLICT, "같은 이름이 이미 있습니다")
    src = _safe_abs(root.abs_path, f.rel_path)
    os.rename(src, dst)
    f.rel_path = new_rel
    f.filename = new_name
    f.ext = new_name.rsplit(".", 1)[1].lower() if "." in new_name else ""
    db.commit()
    fts.rebuild_file(db, f.id)
    db.commit()
    return _file_out(f)


class DeleteFilesIn(BaseModel):
    file_ids: list[int]


@router.post("/delete")
def delete_files(
    payload: DeleteFilesIn,
    db: Session = Depends(get_db),
    user: User = Depends(require_can_delete),
):
    """Permanently delete files from disk + catalog (writable roots only)."""
    deleted = 0
    for fid in payload.file_ids:
        f = db.get(File, fid)
        if f is None or f.status != "active":
            continue
        root = db.get(Root, f.root_id)
        if root is None or root.readonly:
            continue
        if effective_folder_level(db, user, f.root_id, _parent_dir(f.rel_path)) == "hidden":
            continue
        abs_p = _safe_abs(root.abs_path, f.rel_path)
        try:
            if abs_p.exists():
                os.remove(abs_p)
        except OSError:
            pass
        fts.delete_file(db, f.id)
        ft = db.get(FileText, f.id)
        if ft is not None:
            db.delete(ft)
        db.delete(f)
        deleted += 1
    db.commit()
    return {"deleted": deleted}


class TransferFilesIn(BaseModel):
    file_ids: list[int]
    dest_root_id: int
    dest_path: str = ""   # target folder within the destination root ("" = root)


def _unique_dest(db: Session, dest_root_id: int, root_abs: str,
                 folder: str, name: str) -> tuple[str, str]:
    """Pick a rel_path under `folder` that collides with neither an active
    File row nor an on-disk entry — appending ' (1)', ' (2)', … before the
    extension the way file managers do. Returns (rel_path, final_name)."""
    if "." in name and not name.startswith("."):
        stem, ext = name.rsplit(".", 1)
        suffix = "." + ext
    else:
        stem, suffix = name, ""
    base = (folder + "/") if folder else ""
    i = 0
    while True:
        cand = name if i == 0 else f"{stem} ({i}){suffix}"
        rel = base + cand
        in_db = db.execute(
            select(File.id).where(
                File.root_id == dest_root_id, File.rel_path == rel,
                File.status == "active")
        ).scalar_one_or_none()
        if in_db is None and not _safe_abs(root_abs, rel).exists():
            return rel, cand
        i += 1


def _transfer(db: Session, user: User, payload: TransferFilesIn, *, move: bool) -> dict:
    """Shared copy/move core. Destination must be a writable file root; for a
    move the source root must be writable too (can't remove from a read-only
    mount). Cross-root transfers are allowed. Collisions get a ' (n)' suffix.
    """
    dest_root = _writable_file_root(db, payload.dest_root_id)
    dest_folder = _norm_folder(payload.dest_path)
    if effective_folder_level(db, user, dest_root.id, dest_folder) == "hidden":
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    dest_dir = _safe_abs(dest_root.abs_path, dest_folder)
    os.makedirs(dest_dir, exist_ok=True)

    import mimetypes
    from datetime import datetime as _dt

    done = 0
    skipped = 0
    touched: list[int] = []
    for fid in payload.file_ids:
        f = db.get(File, fid)
        if f is None or f.status != "active":
            skipped += 1
            continue
        src_root = db.get(Root, f.root_id)
        if src_root is None:
            skipped += 1
            continue
        # Moving needs write access to the source; copying can read from RO.
        if move and src_root.readonly:
            skipped += 1
            continue
        if effective_folder_level(
                db, user, f.root_id, _parent_dir(f.rel_path)) == "hidden":
            skipped += 1
            continue
        # No-op move into the same folder.
        if move and f.root_id == dest_root.id and _parent_dir(f.rel_path) == dest_folder:
            skipped += 1
            continue
        src_abs = _safe_abs(src_root.abs_path, f.rel_path)
        if not src_abs.exists():
            skipped += 1
            continue
        rel, final_name = _unique_dest(
            db, dest_root.id, dest_root.abs_path, dest_folder, f.filename)
        dst_abs = _safe_abs(dest_root.abs_path, rel)
        try:
            if move:
                shutil.move(str(src_abs), str(dst_abs))
            else:
                shutil.copy2(str(src_abs), str(dst_abs))
        except OSError as e:
            raise HTTPException(status.HTTP_409_CONFLICT, f"{f.filename}: {e}")

        ext = final_name.rsplit(".", 1)[1].lower() if "." in final_name else ""
        st = os.stat(dst_abs)
        if move:
            # Relocate the existing row (content unchanged → keep sha/text).
            f.root_id = dest_root.id
            f.rel_path = rel
            f.parent = dest_folder
            f.filename = final_name
            f.ext = ext
            f.status = "active"
            db.flush()
            touched.append(f.id)
            fts.rebuild_file(db, f.id)
        else:
            row = File(
                root_id=dest_root.id, rel_path=rel, filename=final_name, ext=ext,
                parent=dest_folder, mime=mimetypes.guess_type(final_name)[0],
                file_size=st.st_size, mtime=_dt.fromtimestamp(st.st_mtime),
                content_signature=f"{st.st_size}:{st.st_mtime_ns}",
                status="active", text_status="pending", owner_user_id=user.id)
            db.add(row)
            db.flush()
            _enqueue_index(db, row.id)
            touched.append(row.id)
        done += 1
    db.commit()
    if not move and touched:
        fts.bulk_rebuild_files(db, touched)
        db.commit()
    return {"moved" if move else "copied": done, "skipped": skipped}


@router.post("/copy")
def copy_files(
    payload: TransferFilesIn,
    db: Session = Depends(get_db),
    user: User = Depends(require_can_upload),
):
    """Copy files into a writable folder (source may be read-only)."""
    return _transfer(db, user, payload, move=False)


@router.post("/move")
def move_files(
    payload: TransferFilesIn,
    db: Session = Depends(get_db),
    user: User = Depends(require_can_delete),
):
    """Move files into a writable folder (source must be writable too)."""
    return _transfer(db, user, payload, move=True)

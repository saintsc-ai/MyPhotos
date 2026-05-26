"""Folder CRUD on writable roots.

Listing the tree itself stays in app.api.routes_photos.list_folders —
it's read-only and consumed by every authenticated user. The
mutating endpoints live here because they're admin-only and only
make sense when root.readonly is false.

Safety properties enforced everywhere:
  - root.readonly must be false
  - rel_path must resolve inside the root directory (no traversal)
  - folder name forbids OS-illegal characters and the reserved
    "." / ".." entries
  - delete refuses non-empty folders unless `recursive=true`, and
    even then routes the photos through the regular trash flow
"""

from __future__ import annotations

import logging
import os
import shutil
from datetime import datetime
from pathlib import Path

from fastapi import (
    APIRouter, Body, Depends, File, Form, HTTPException, UploadFile, status,
)
from pydantic import BaseModel, Field
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from ..api.deps import get_db
from ..auth import require_admin
from ..models import Photo, Root, User
from ..scanner.utils import classify, nfc

log = logging.getLogger(__name__)

router = APIRouter(prefix="/admin/folders", tags=["admin", "folders"])

# OS-illegal in filenames on every common FS we target.
_ILLEGAL = set('/\\:*?"<>|')


def _ensure_writable_root(db: Session, root_id: int) -> Root:
    root = db.get(Root, root_id)
    if root is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "root not found")
    if root.readonly:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"root '{root.label}'는 읽기 전용입니다 — 관리 → 사진 폴더에서 RO 토글을 끄세요",
        )
    return root


def _safe_join(root_abs: str, rel: str) -> Path:
    """Resolve rel under root_abs; refuse anything that escapes."""
    root_abs_p = Path(root_abs).resolve()
    candidate = (root_abs_p / rel).resolve()
    try:
        candidate.relative_to(root_abs_p)
    except ValueError:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, "경로가 root 밖을 가리킵니다"
        )
    return candidate


def _safe_folder_name(raw: str) -> str:
    n = nfc((raw or "").strip())
    if not n:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "폴더 이름이 비어있습니다")
    if n in (".", ".."):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "사용할 수 없는 이름")
    for c in n:
        if c in _ILLEGAL:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                f"폴더 이름에 쓸 수 없는 문자가 있습니다: {c!r}",
            )
    return n


# ---------- models ----------

class CreateIn(BaseModel):
    root_id: int
    parent_rel_path: str = Field(
        default="", description="새 폴더가 만들어질 부모 폴더의 rel_path. 빈 값=root 직속"
    )
    name: str


class CreateResult(BaseModel):
    ok: bool
    rel_path: str


class RenameIn(BaseModel):
    root_id: int
    rel_path: str
    new_name: str


class RenameResult(BaseModel):
    ok: bool
    old_rel_path: str
    new_rel_path: str
    moved_photos: int


class DeleteIn(BaseModel):
    root_id: int
    rel_path: str
    recursive: bool = False


class DeleteResult(BaseModel):
    ok: bool
    trashed_photos: int


# ---------- endpoints ----------

@router.post("", response_model=CreateResult, status_code=status.HTTP_201_CREATED)
def create_folder(
    body: CreateIn,
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
) -> CreateResult:
    root = _ensure_writable_root(db, body.root_id)
    name = _safe_folder_name(body.name)
    parent = nfc((body.parent_rel_path or "").strip("/"))
    new_rel = f"{parent}/{name}" if parent else name
    abs_path = _safe_join(root.abs_path, new_rel)
    if abs_path.exists():
        raise HTTPException(status.HTTP_409_CONFLICT, "이미 같은 이름의 폴더가 있습니다")
    try:
        abs_path.mkdir(parents=True, exist_ok=False)
    except OSError as e:
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR, f"폴더 생성 실패: {e}"
        )
    return CreateResult(ok=True, rel_path=new_rel)


@router.patch("/rename", response_model=RenameResult)
def rename_folder(
    body: RenameIn,
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
) -> RenameResult:
    root = _ensure_writable_root(db, body.root_id)
    new_name = _safe_folder_name(body.new_name)
    old_rel = nfc(body.rel_path.strip("/"))
    if not old_rel:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "root 자체는 이름을 바꿀 수 없습니다")
    old_abs = _safe_join(root.abs_path, old_rel)
    if not old_abs.is_dir():
        raise HTTPException(status.HTTP_404_NOT_FOUND, "폴더를 찾을 수 없습니다")
    parent = "/".join(old_rel.split("/")[:-1])
    new_rel = f"{parent}/{new_name}" if parent else new_name
    if new_rel == old_rel:
        return RenameResult(ok=True, old_rel_path=old_rel, new_rel_path=new_rel, moved_photos=0)
    new_abs = _safe_join(root.abs_path, new_rel)
    if new_abs.exists():
        raise HTTPException(status.HTTP_409_CONFLICT, "같은 이름의 폴더가 이미 있습니다")
    try:
        old_abs.rename(new_abs)
    except OSError as e:
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, f"이름 변경 실패: {e}")
    # Update DB rel_paths for every photo under the old prefix.
    old_prefix = old_rel + "/"
    new_prefix = new_rel + "/"
    res = db.execute(
        text(
            "UPDATE photos "
            "SET rel_path = :np || substr(rel_path, :off) "
            "WHERE root_id = :rid AND rel_path LIKE :pat"
        ),
        {
            "np": new_prefix,
            "off": len(old_prefix) + 1,    # 1-based, sqlite substr() offset
            "rid": root.id,
            "pat": old_prefix + "%",
        },
    )
    db.commit()
    return RenameResult(
        ok=True,
        old_rel_path=old_rel,
        new_rel_path=new_rel,
        moved_photos=res.rowcount or 0,
    )


@router.delete("", response_model=DeleteResult)
def delete_folder(
    body: DeleteIn = Body(...),
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
) -> DeleteResult:
    root = _ensure_writable_root(db, body.root_id)
    rel = nfc(body.rel_path.strip("/"))
    if not rel:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "root 자체는 삭제할 수 없습니다")
    abs_path = _safe_join(root.abs_path, rel)
    if not abs_path.is_dir():
        raise HTTPException(status.HTTP_404_NOT_FOUND, "폴더를 찾을 수 없습니다")

    # How many active photos live under this folder (recursively)?
    like_pat = rel + "/%"
    photo_count = db.execute(
        select(func.count(Photo.id)).where(
            Photo.root_id == root.id,
            Photo.rel_path.like(like_pat),
            Photo.status == "active",
        )
    ).scalar_one()

    if photo_count > 0 and not body.recursive:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"폴더 안에 사진 {photo_count}개가 있습니다. "
            f"recursive=true로 호출하면 모두 휴지통으로 이동 후 폴더가 지워집니다.",
        )

    trashed = 0
    if body.recursive and photo_count > 0:
        # Reuse the existing trash move so the user can restore each file
        # individually from the 휴지통 tab.
        from ..api.routes_photos import _move_to_trash
        rows = db.execute(
            select(Photo).where(
                Photo.root_id == root.id,
                Photo.rel_path.like(like_pat),
                Photo.status == "active",
            )
        ).scalars().all()
        for p in rows:
            try:
                _move_to_trash(p, root, user)
            except Exception:
                pass
            if p.status != "trashed":
                p.status = "trashed"
            trashed += 1
        db.commit()

    # Now remove the (hopefully empty) folder. rmtree() handles any
    # leftover non-photo files (e.g. .DS_Store) that the trash move
    # didn't touch.
    try:
        shutil.rmtree(abs_path)
    except OSError as e:
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, f"삭제 실패: {e}")

    return DeleteResult(ok=True, trashed_photos=trashed)


# ---------- upload ----------

class UploadResult(BaseModel):
    saved: list[str]
    skipped: list[str]
    count: int


@router.post("/upload", response_model=UploadResult)
async def upload_files(
    root_id: int = Form(...),
    rel_path: str = Form(""),
    files: list[UploadFile] = File(...),
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
) -> UploadResult:
    """Save one or more files into (root_id, rel_path). The scanner
    notices them on the next pass (or the watcher kicks immediately
    if enabled), so a `discover_root` job is enqueued to index them
    promptly without waiting for the daily APScheduler tick.

    Filenames go through NFC normalisation and the same extension
    allowlist as the scanner — anything else lands in `skipped` so
    the user gets a clear "this format wasn't accepted" message
    instead of a half-indexed mess on disk.
    """
    root = _ensure_writable_root(db, root_id)
    rel = nfc(rel_path.strip("/"))
    target_dir = _safe_join(root.abs_path, rel) if rel else Path(root.abs_path).resolve()
    if not target_dir.is_dir():
        raise HTTPException(status.HTTP_404_NOT_FOUND, "대상 폴더가 없습니다")

    saved: list[str] = []
    skipped: list[str] = []
    for upload in files:
        raw_name = upload.filename or ""
        name = nfc(raw_name)
        # Strip any path components the browser sent (some browsers
        # include subpaths from webkitdirectory uploads). Only the
        # final segment lands here; nested upload would need a
        # different endpoint.
        name = name.replace("\\", "/").rsplit("/", 1)[-1].strip()
        if not name or any(c in name for c in _ILLEGAL):
            skipped.append(raw_name or "(이름 없음)")
            continue
        kind, _ext = classify(name)
        if kind is None:
            # Not an image / video extension we index.
            skipped.append(name)
            continue
        dest = target_dir / name
        if dest.exists():
            # Don't clobber — disambiguate with a timestamp.
            ts = datetime.utcnow().strftime("%Y%m%d%H%M%S")
            if "." in name:
                stem, _dot, ext = name.rpartition(".")
                new_name = f"{stem}_{ts}.{ext}"
            else:
                new_name = f"{name}_{ts}"
            dest = target_dir / new_name
        try:
            with dest.open("wb") as f:
                while True:
                    chunk = await upload.read(1024 * 1024)
                    if not chunk:
                        break
                    f.write(chunk)
            saved.append(dest.name)
        except Exception as e:
            log.warning("upload write failed for %s: %s", dest, e)
            skipped.append(name)
            try:
                dest.unlink(missing_ok=True)
            except OSError:
                pass

    if saved:
        # Queue a (limit-less) discover_root so the new files get
        # picked up promptly. priority 15 sits between the daily
        # tick (10) and admin-triggered scans (20).
        from ..worker.jobs import enqueue
        enqueue(
            db,
            kind="discover_root",
            payload={"root_id": root_id},
            priority=15,
        )
        db.commit()

    return UploadResult(saved=saved, skipped=skipped, count=len(saved))

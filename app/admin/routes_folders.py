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

from .. import audit
from ..api.deps import get_db
from ..auth import (
    require_admin,
    require_can_delete,
    require_can_edit_meta_others,
    require_can_upload,
)
from ..auth_acl import require_folder_level
from ..models import FolderACL, Photo, Root, UploadPending, User
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
    user: User = Depends(require_can_upload),
    db: Session = Depends(get_db),
) -> CreateResult:
    parent = nfc((body.parent_rel_path or "").strip("/"))
    # `can_upload` (Depends above) is the real authorization gate; the
    # folder-level guard only excludes read-only / hidden folders so a
    # user with the flag can act on any folder they have normal access to.
    require_folder_level(db, user, body.root_id, parent, "interact")
    root = _ensure_writable_root(db, body.root_id)
    name = _safe_folder_name(body.name)
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
    user: User = Depends(require_can_edit_meta_others),
    db: Session = Depends(get_db),
) -> RenameResult:
    old_rel_check = nfc((body.rel_path or "").strip("/"))
    # `can_edit_meta_others` is the real gate; folder guard only
    # excludes restricted-access folders. See create_folder for rationale.
    require_folder_level(db, user, body.root_id, old_rel_check, "interact")
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
    user: User = Depends(require_can_delete),
    db: Session = Depends(get_db),
) -> DeleteResult:
    rel_check = nfc((body.rel_path or "").strip("/"))
    # `can_delete` is the real gate; folder guard only excludes
    # restricted-access folders. See create_folder for rationale.
    require_folder_level(db, user, body.root_id, rel_check, "interact")
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
    failed_moves: list[tuple[int, str]] = []
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
                result = _move_to_trash(p, root, user)
            except Exception as e:
                failed_moves.append((p.id, str(e)[:200]))
                continue
            # Only flip status when the file is actually safe in the
            # trash. Otherwise the rmtree below would silently destroy
            # the original — that was the bug: status='trashed' but the
            # file is gone, and the user can't restore it.
            if result.get("moved"):
                p.status = "trashed"
                trashed += 1
            else:
                failed_moves.append((p.id, result.get("reason") or "unknown"))
        db.commit()

    # Abort if any file failed to reach the trash — rmtree would
    # permanently destroy the originals otherwise.
    if failed_moves:
        sample = ", ".join(f"#{pid}: {reason}" for pid, reason in failed_moves[:3])
        more = f" 외 {len(failed_moves) - 3}건" if len(failed_moves) > 3 else ""
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"휴지통으로 옮기지 못한 사진 {len(failed_moves)}건이 있어 폴더 삭제를 중단했습니다 "
            f"({sample}{more}). 휴지통 용량을 확보한 뒤 다시 시도하세요. "
            f"이미 휴지통으로 옮긴 {trashed}건은 휴지통에서 복구 가능합니다.",
        )

    # All photos safely in trash; remove the (now mostly empty) folder.
    # rmtree handles leftover non-photo files (e.g. .DS_Store) that the
    # trash move didn't touch.
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
    user: User = Depends(require_can_upload),
    db: Session = Depends(get_db),
) -> UploadResult:
    # `can_upload` is the real gate; folder guard only excludes
    # restricted-access folders. See create_folder for rationale.
    require_folder_level(db, user, root_id, nfc((rel_path or "").strip("/")), "interact")
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
            # Record uploader so index_file can stamp Photo.owner_user_id
            # when the matching row is created. rel_path here is the
            # full POSIX path matching Photo.rel_path exactly.
            file_rel = f"{rel}/{dest.name}" if rel else dest.name
            existing_pending = db.execute(
                select(UploadPending).where(
                    UploadPending.root_id == root_id,
                    UploadPending.rel_path == file_rel,
                )
            ).scalar_one_or_none()
            if existing_pending is None:
                db.add(UploadPending(
                    root_id=root_id, rel_path=file_rel, user_id=user.id,
                ))
            else:
                # Last-writer-wins on collision (timestamp suffix in
                # dest.name normally makes this unreachable).
                existing_pending.user_id = user.id
                existing_pending.created_at = datetime.utcnow()
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


# ---------- folder ACL (P3 of access control) ----------
#
# Admin assigns per-folder overrides on top of root-level ACL.
# path_prefix is stored with a trailing slash so LIKE 'prefix%'
# never accidentally matches 'prefix2'. Empty prefix is rejected
# (use root_acl instead for whole-root rules).

_ACL_LEVELS = ("hidden", "read", "interact", "contribute", "manage")


def _normalize_prefix(raw: str) -> str:
    p = nfc((raw or "").strip().strip("/"))
    if not p:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "폴더 prefix가 비어있습니다 — 전체 root 규칙은 root_acl을 사용하세요",
        )
    return p + "/"


class FolderACLEntryOut(BaseModel):
    root_id: int
    path_prefix: str    # always trailing slash
    user_id: int
    username: str
    is_admin: bool
    level: str


class FolderACLEntryIn(BaseModel):
    user_id: int
    path_prefix: str = Field(min_length=1)
    level: str = Field(pattern=r"^(hidden|read|interact|contribute|manage)$")


@router.get("/{root_id}/acl", response_model=list[FolderACLEntryOut])
def list_folder_acl(
    root_id: int,
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
) -> list[FolderACLEntryOut]:
    """Every folder_acl row for a root, joined with username. Sorted by
    (path_prefix, user_id) so the admin UI can render the entries
    grouped by folder.
    """
    if db.get(Root, root_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "root not found")
    rows = db.execute(
        select(FolderACL, User)
        .join(User, User.id == FolderACL.user_id)
        .where(FolderACL.root_id == root_id)
        .order_by(FolderACL.path_prefix, FolderACL.user_id)
    ).all()
    return [
        FolderACLEntryOut(
            root_id=fa.root_id,
            path_prefix=fa.path_prefix,
            user_id=u.id,
            username=u.username,
            is_admin=bool(u.is_admin),
            level=fa.level,
        )
        for fa, u in rows
    ]


@router.put("/{root_id}/acl", status_code=status.HTTP_204_NO_CONTENT)
def set_folder_acl(
    root_id: int,
    entry: FolderACLEntryIn,
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
) -> None:
    """Upsert a single (root_id, path_prefix, user_id) row. path_prefix
    is normalized to NFC + trailing slash so reads use a uniform
    LIKE pattern.
    """
    if db.get(Root, root_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "root not found")
    target = db.get(User, entry.user_id)
    if target is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "user not found")
    if entry.level not in _ACL_LEVELS:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "invalid level")
    prefix = _normalize_prefix(entry.path_prefix)
    existing = db.get(FolderACL, (root_id, prefix, entry.user_id))
    before = existing.level if existing else None
    if existing is None:
        db.add(FolderACL(
            root_id=root_id,
            path_prefix=prefix,
            user_id=entry.user_id,
            level=entry.level,
        ))
    else:
        existing.level = entry.level
    audit.record(
        db, user, "acl.folder.set", "folder", f"{root_id}:{prefix}",
        detail={"target_user": target.username, "target_id": target.id,
                "before": before, "after": entry.level},
    )
    db.commit()


@router.delete("/{root_id}/acl", status_code=status.HTTP_204_NO_CONTENT)
def delete_folder_acl(
    root_id: int,
    user_id: int,
    path_prefix: str,
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
) -> None:
    """Drop one folder_acl row. user_id + path_prefix come as query
    params since DELETE has no body in most clients.
    """
    prefix = _normalize_prefix(path_prefix)
    row = db.get(FolderACL, (root_id, prefix, user_id))
    if row is None:
        return  # idempotent
    target = db.get(User, user_id)
    audit.record(
        db, user, "acl.folder.delete", "folder", f"{root_id}:{prefix}",
        detail={"target_user": target.username if target else None,
                "target_id": user_id, "before": row.level},
    )
    db.delete(row)
    db.commit()

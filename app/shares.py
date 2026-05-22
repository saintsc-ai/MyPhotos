"""Public photo share links + per-user management.

Two routers:
- `public_router`  — mounted at /api/share. No auth: the token IS the secret.
                      Returns 401 if the share has a password and the caller
                      hasn't unlocked it in this browser session.
- `admin_router`   — mounted at /api/shares. Requires login. Owner can list,
                      create, and revoke their share links.

Unlocked-share tokens are stashed in the existing signed session cookie
(SessionMiddleware) so we don't need a second table to track sessions.
"""

from __future__ import annotations

import secrets
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from .api.deps import get_db
from .auth import hash_password, require_auth, verify_password
from .config import get_settings
from .models import Photo, Root, Share, User
from .scanner.utils import join_root
from .worker.thumbs import thumb_path

SESSION_UNLOCKED = "unlocked_shares"
TOKEN_BYTES = 18  # ≈ 24-char urlsafe string


# ----- helpers -----

def _new_token() -> str:
    return secrets.token_urlsafe(TOKEN_BYTES)


def _is_active(s: Share) -> bool:
    if s.revoked_at is not None:
        return False
    if s.expires_at is not None and s.expires_at < datetime.utcnow():
        return False
    return True


def _is_unlocked(s: Share, request: Request) -> bool:
    if s.password_hash is None:
        return True
    return s.token in request.session.get(SESSION_UNLOCKED, [])


def _resolve(token: str, db: Session) -> Share:
    s = db.execute(select(Share).where(Share.token == token)).scalar_one_or_none()
    if s is None or not _is_active(s):
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, "공유 링크를 찾을 수 없거나 만료되었습니다"
        )
    return s


# ----- DTOs -----

class ShareCreateIn(BaseModel):
    photo_id: int
    password: Optional[str] = None
    expires_in_days: Optional[int] = Field(default=None, ge=0, le=3650)
    title: Optional[str] = None


class ShareOut(BaseModel):
    id: int
    token: str
    url_path: str
    photo_id: int
    has_password: bool
    title: Optional[str]
    expires_at: Optional[datetime]
    view_count: int
    created_at: datetime
    revoked: bool


class PublicPhotoInfo(BaseModel):
    id: int
    filename: str
    taken_at: Optional[datetime]
    camera_model: Optional[str]
    width: Optional[int]
    height: Optional[int]
    media_kind: str


class PublicShareOut(BaseModel):
    token: str
    title: Optional[str]
    needs_password: bool
    expires_at: Optional[datetime]
    photo: Optional[PublicPhotoInfo]  # populated only when unlocked


class UnlockIn(BaseModel):
    password: str


def _to_share_out(s: Share) -> ShareOut:
    return ShareOut(
        id=s.id,
        token=s.token,
        url_path=f"/share.html?t={s.token}",
        photo_id=s.photo_id,
        has_password=s.password_hash is not None,
        title=s.title,
        expires_at=s.expires_at,
        view_count=s.view_count,
        created_at=s.created_at,
        revoked=s.revoked_at is not None,
    )


# ----- admin router (auth required; mounted with global require_auth dep) -----

admin_router = APIRouter(prefix="/shares", tags=["shares"])


@admin_router.get("", response_model=list[ShareOut])
def list_shares(db: Session = Depends(get_db)) -> list[ShareOut]:
    rows = db.execute(
        select(Share).order_by(Share.created_at.desc())
    ).scalars().all()
    return [_to_share_out(s) for s in rows]


@admin_router.post("", response_model=ShareOut)
def create_share(
    payload: ShareCreateIn,
    db: Session = Depends(get_db),
    user: User = Depends(require_auth),
) -> ShareOut:
    p = db.get(Photo, payload.photo_id)
    if p is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "사진을 찾을 수 없습니다")

    expires_at = None
    if payload.expires_in_days is not None and payload.expires_in_days > 0:
        expires_at = datetime.utcnow() + timedelta(days=payload.expires_in_days)

    s = Share(
        token=_new_token(),
        photo_id=payload.photo_id,
        title=payload.title,
        password_hash=hash_password(payload.password) if payload.password else None,
        expires_at=expires_at,
        created_by_user_id=user.id,
    )
    db.add(s)
    db.commit()
    db.refresh(s)
    return _to_share_out(s)


@admin_router.delete("/{share_id}")
def revoke_share(share_id: int, db: Session = Depends(get_db)) -> dict:
    s = db.get(Share, share_id)
    if s is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    if s.revoked_at is None:
        s.revoked_at = datetime.utcnow()
        db.commit()
    return {"ok": True}


# ----- public router (no auth) -----

public_router = APIRouter(prefix="/share", tags=["share-public"])


@public_router.get("/{token}", response_model=PublicShareOut)
def get_public_share(
    token: str, request: Request, db: Session = Depends(get_db)
) -> PublicShareOut:
    s = _resolve(token, db)
    unlocked = _is_unlocked(s, request)
    out = PublicShareOut(
        token=s.token,
        title=s.title,
        needs_password=(s.password_hash is not None) and not unlocked,
        expires_at=s.expires_at,
        photo=None,
    )
    if unlocked:
        p = db.get(Photo, s.photo_id)
        if p is not None:
            out.photo = PublicPhotoInfo(
                id=p.id,
                filename=p.filename,
                taken_at=p.taken_at,
                camera_model=p.camera_model,
                width=p.width,
                height=p.height,
                media_kind=p.media_kind,
            )
            s.view_count += 1
            db.commit()
    return out


@public_router.post("/{token}/unlock")
def unlock(
    token: str,
    payload: UnlockIn,
    request: Request,
    db: Session = Depends(get_db),
) -> dict:
    s = _resolve(token, db)
    if s.password_hash is None:
        return {"ok": True, "needs_password": False}
    if not verify_password(payload.password, s.password_hash):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "암호가 올바르지 않습니다")
    unlocked = list(request.session.get(SESSION_UNLOCKED, []))
    if s.token not in unlocked:
        unlocked.append(s.token)
        # Keep the cookie bounded — drop the oldest if it grows past 50 entries.
        if len(unlocked) > 50:
            unlocked = unlocked[-50:]
        request.session[SESSION_UNLOCKED] = unlocked
    return {"ok": True}


@public_router.get("/{token}/thumb")
def get_share_thumb(
    token: str,
    request: Request,
    size: int = Query(256),
    db: Session = Depends(get_db),
) -> FileResponse:
    s = _resolve(token, db)
    if not _is_unlocked(s, request):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "암호 필요")
    p = db.get(Photo, s.photo_id)
    if p is None or not p.sha256:
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    settings = get_settings()
    sizes = sorted(settings.thumbnails.sizes)
    chosen = next((sz for sz in sizes if sz >= size), sizes[-1])
    path = thumb_path(p.sha256, chosen)
    if not path.exists():
        # Fall back to any size that actually exists.
        for sz in reversed(sizes):
            alt = thumb_path(p.sha256, sz)
            if alt.exists():
                path = alt
                break
        else:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "썸네일 미생성")
    return FileResponse(path, media_type="image/jpeg")


@public_router.get("/{token}/original")
def get_share_original(
    token: str, request: Request, db: Session = Depends(get_db)
) -> FileResponse:
    s = _resolve(token, db)
    if not _is_unlocked(s, request):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "암호 필요")
    p = db.get(Photo, s.photo_id)
    if p is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    root = db.get(Root, p.root_id)
    if root is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "root missing")
    src = Path(join_root(root.abs_path, p.rel_path))
    if not src.exists():
        raise HTTPException(status.HTTP_404_NOT_FOUND, "원본 파일이 사라졌습니다")
    return FileResponse(src, filename=p.filename)

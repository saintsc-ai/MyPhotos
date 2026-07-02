"""Public photo share links + per-user management.

Two routers:
- `public_router`  — mounted at /api/share. No auth: the token IS the secret.
                      Returns 401 if the share has a password and the caller
                      hasn't unlocked it in this browser session.
- `admin_router`   — mounted at /api/shares. Requires login. Owner can list,
                      create, and revoke their share links.

Unlocked-share tokens are stashed in the existing signed session cookie
(SessionMiddleware) so we don't need a second table to track sessions.

A share holds 1+ photos via the `share_items` table. The legacy single
`Share.photo_id` column is still honoured for older rows; new shares
always write share_items rows.
"""

from __future__ import annotations

import io
import secrets
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import FileResponse, Response, StreamingResponse

from .http_headers import content_disposition
from pydantic import BaseModel, Field
from sqlalchemy import and_, case, func, or_, select
from sqlalchemy.orm import Session

from .api.deps import get_db
from . import audit
from .auth import hash_password, require_auth, require_can_share, verify_password
from .auth_acl import effective_folder_level, require_photo_ids_level
from .config import get_settings
from .models import File, Photo, Root, Share, ShareFileItem, ShareItem, User
from .scanner.utils import join_root
from .worker.thumbs import thumb_path

SESSION_UNLOCKED = "unlocked_shares"
TOKEN_BYTES = 18  # ≈ 24-char urlsafe string
# No hard cap on photos per share — share_items is a relational table,
# so even a 50k-photo folder share is one row per photo, paginated by
# the public viewer. Earlier 1000-cap was a defensive limit before the
# UI supported folder-level sharing.


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


def _share_photos(s: Share, db: Session) -> list[Photo]:
    """All photos in a share, ordered by sort_idx then by taken_at desc.

    Falls back to the legacy `Share.photo_id` for shares created before
    share_items existed.
    """
    rows = db.execute(
        select(Photo)
        .join(ShareItem, ShareItem.photo_id == Photo.id)
        .where(ShareItem.share_id == s.id)
        .order_by(
            ShareItem.sort_idx,
            Photo.taken_at.desc().nullslast(),
            Photo.mtime.desc().nullslast(),
            Photo.id.desc(),
        )
    ).scalars().all()
    if rows:
        return list(rows)
    if s.photo_id:
        p = db.get(Photo, s.photo_id)
        if p is not None:
            return [p]
    return []


def _verify_photo_in_share(s: Share, photo_id: int, db: Session) -> Photo:
    """Return the Photo when (share, photo_id) is a legitimate pair."""
    in_items = db.execute(
        select(ShareItem).where(
            ShareItem.share_id == s.id,
            ShareItem.photo_id == photo_id,
        )
    ).first()
    if in_items is None and s.photo_id != photo_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "사진이 이 공유에 없습니다")
    p = db.get(Photo, photo_id)
    if p is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "사진을 찾을 수 없습니다")
    return p


def _share_files(s: Share, db: Session) -> list[File]:
    """Files in a share via share_file_items (files domain)."""
    return db.execute(
        select(File)
        .join(ShareFileItem, ShareFileItem.file_id == File.id)
        .where(ShareFileItem.share_id == s.id)
        .order_by(ShareFileItem.sort_idx, ShareFileItem.file_id)
    ).scalars().all()


def _verify_file_in_share(s: Share, file_id: int, db: Session) -> File:
    """Return the File when (share, file_id) is a legitimate pair."""
    row = db.execute(
        select(ShareFileItem).where(
            ShareFileItem.share_id == s.id, ShareFileItem.file_id == file_id,
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    f = db.get(File, file_id)
    if f is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    return f


def _check_download_quota(s: Share) -> None:
    """Raise 410 Gone when the share has hit its max-downloads cap.

    Cheap pre-flight check used to fail fast without holding a row.
    The actual quota enforcement is done atomically by
    _atomic_consume_download below — this is just for nicer error
    messages before we spend time building the response.
    """
    if s.max_downloads is not None and s.download_count >= s.max_downloads:
        raise HTTPException(
            status.HTTP_410_GONE,
            f"다운로드 횟수 초과 ({s.max_downloads}회). 공유자에게 문의하세요.",
        )


def _atomic_consume_download(s: Share, db: Session) -> None:
    """Race-safe download-counter bump.

    Previously: check (`download_count < max_downloads`) → build zip
    (minutes) → bump counter. Two concurrent requests could both pass
    the check and both stream the bundle, blowing past a
    `max_downloads=1` cap.

    Now: single UPDATE with WHERE that enforces the cap. If 0 rows
    matched, the cap was already reached — raise 410 BEFORE the
    response body is built. Caller must invoke this *before* spending
    real work (zip build / large stream).
    """
    from sqlalchemy import update
    if s.max_downloads is not None:
        res = db.execute(
            update(Share)
            .where(
                Share.id == s.id,
                Share.download_count < Share.max_downloads,
            )
            .values(download_count=Share.download_count + 1)
        )
        db.commit()
        if res.rowcount == 0:
            # Lost the race or already capped.
            raise HTTPException(
                status.HTTP_410_GONE,
                f"다운로드 횟수 초과 ({s.max_downloads}회). 공유자에게 문의하세요.",
            )
    else:
        # No cap — just bump.
        db.execute(
            update(Share)
            .where(Share.id == s.id)
            .values(download_count=Share.download_count + 1)
        )
        db.commit()
    # Keep the in-memory object in sync for any subsequent reads in
    # this request (e.g. building the response payload).
    s.download_count = (s.download_count or 0) + 1


# Kept for the few callers that intentionally want a non-atomic bump
# (e.g. view_count which has no cap). Prefer _atomic_consume_download.
def _consume_download(s: Share, db: Session) -> None:
    s.download_count = (s.download_count or 0) + 1
    db.commit()


_GPS_IFD_TAG = 0x8825  # ExifTags.IFD.GPSInfo


def _strip_gps_jpeg(src_path: Path) -> Optional[bytes]:
    """Return JPEG bytes with the GPSInfo IFD removed, or None when the
    file isn't a JPEG we can safely round-trip.

    Re-saves the pixel data via Pillow with `exif=` set to the original
    EXIF blob minus the GPSInfo sub-IFD. Other identifying tags
    (Make/Model/serial/lens) are deliberately preserved — the user opts
    into stripping *location*, not all metadata.
    """
    try:
        from PIL import Image as _PILImage
        _PILImage.MAX_IMAGE_PIXELS = 64_000_000
        with _PILImage.open(src_path) as im:
            if (im.format or "").upper() != "JPEG":
                return None
            exif = im.getexif()
            if _GPS_IFD_TAG in exif:
                del exif[_GPS_IFD_TAG]
            buf = io.BytesIO()
            im.save(buf, format="JPEG", quality="keep", exif=exif.tobytes())
            return buf.getvalue()
    except Exception:
        # Bad EXIF, truncated file, etc. — fall back to streaming the
        # original. We'd rather serve the file with GPS than 500.
        return None


def _maybe_strip_response(
    s: Share, p: Photo, src: Path
) -> Optional[Response]:
    """When the share has strip_exif on, return an in-memory Response
    with GPS scrubbed. Returns None to mean "stream the original file
    as-is" (non-JPEG, or strip not requested)."""
    if not getattr(s, "strip_exif", False):
        return None
    ext = (p.filename.rsplit(".", 1)[-1] if "." in p.filename else "").lower()
    if ext not in ("jpg", "jpeg"):
        return None
    data = _strip_gps_jpeg(src)
    if data is None:
        return None
    headers = {
        "Content-Disposition": content_disposition("attachment", p.filename),
    }
    return Response(content=data, media_type="image/jpeg", headers=headers)


def _thumb_file(p: Photo, size_hint: int) -> Path:
    if not p.sha256:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "썸네일 미생성")
    settings = get_settings()
    sizes = sorted(settings.thumbnails.sizes)
    chosen = next((sz for sz in sizes if sz >= size_hint), sizes[-1])
    path = thumb_path(p.sha256, chosen)
    if not path.exists():
        for sz in reversed(sizes):
            alt = thumb_path(p.sha256, sz)
            if alt.exists():
                return alt
        raise HTTPException(status.HTTP_404_NOT_FOUND, "썸네일 미생성")
    return path


# ----- DTOs -----

class ShareCreateIn(BaseModel):
    # Either photo_ids (preferred) or the legacy single photo_id is accepted.
    photo_ids: Optional[list[int]] = None
    photo_id: Optional[int] = None
    password: Optional[str] = None
    expires_in_days: Optional[int] = Field(default=None, ge=0, le=3650)
    max_downloads: Optional[int] = Field(default=None, ge=1, le=10000)
    title: Optional[str] = None
    strip_exif: Optional[bool] = False


class ShareOut(BaseModel):
    id: int
    token: str
    url_path: str
    photo_count: int
    # File-domain shares (share_file_items); kind is 'photo' | 'file' | 'mixed'
    # so the admin list can show a generic item count + type.
    file_count: int = 0
    kind: str = "photo"
    has_password: bool
    title: Optional[str]
    expires_at: Optional[datetime]
    max_downloads: Optional[int]
    download_count: int
    view_count: int
    created_at: datetime
    revoked: bool
    # Surfaced so the admin "공유링크" tab can show ownership without
    # a follow-up users fetch. None for legacy rows created before
    # user attribution landed.
    created_by_user_id: Optional[int] = None
    created_by_username: Optional[str] = None
    strip_exif: bool = False


class PublicPhotoInfo(BaseModel):
    id: int
    filename: str
    taken_at: Optional[datetime]
    camera_model: Optional[str]
    width: Optional[int]
    height: Optional[int]
    media_kind: str


class PublicFileInfo(BaseModel):
    id: int
    filename: str
    ext: str
    mime: Optional[str]
    size: Optional[int]


class PublicShareOut(BaseModel):
    token: str
    title: Optional[str]
    needs_password: bool
    expires_at: Optional[datetime]
    photo_count: int
    photos: list[PublicPhotoInfo]  # populated only when unlocked
    # File-domain shares (share_file_items). Empty for photo shares.
    file_count: int = 0
    files: list[PublicFileInfo] = []


class UnlockIn(BaseModel):
    password: str


def _to_share_out(
    s: Share, photo_count: int, username: Optional[str] = None,
    file_count: int = 0,
) -> ShareOut:
    kind = ("mixed" if (photo_count and file_count)
            else "file" if file_count else "photo")
    return ShareOut(
        id=s.id,
        token=s.token,
        url_path=f"/share.html?t={s.token}",
        photo_count=photo_count,
        file_count=file_count,
        kind=kind,
        has_password=s.password_hash is not None,
        title=s.title,
        expires_at=s.expires_at,
        max_downloads=s.max_downloads,
        download_count=s.download_count,
        view_count=s.view_count,
        created_at=s.created_at,
        revoked=s.revoked_at is not None,
        created_by_user_id=s.created_by_user_id,
        created_by_username=username,
        strip_exif=bool(getattr(s, "strip_exif", False)),
    )


# ----- admin router (auth required) -----

admin_router = APIRouter(prefix="/shares", tags=["shares"])


def _require_share_owner_or_admin(s: Share, user: User) -> None:
    """Share endpoints used to only check require_auth, letting any
    logged-in family member edit / revoke / hard-delete each other's
    shares. Enforce ownership here so admins can still moderate but
    non-admins can only touch their own shares.
    """
    if user.is_admin:
        return
    if s.created_by_user_id == user.id:
        return
    raise HTTPException(
        status.HTTP_403_FORBIDDEN,
        "본인이 만든 공유만 수정/취소할 수 있습니다",
    )


@admin_router.get("", response_model=list[ShareOut])
def list_shares(
    db: Session = Depends(get_db),
    user: User = Depends(require_auth),
) -> list[ShareOut]:
    """Full unpaginated list. Kept for back-compat with the original
    admin UI and any external callers; new admin code uses /page below
    so the page can stay responsive past a few thousand shares."""
    q = select(Share).order_by(Share.created_at.desc())
    # Non-admins only see their own shares — listing all shares to
    # everyone leaked the share token (used elsewhere as auth bearer)
    # and made enumeration trivial.
    if not user.is_admin:
        q = q.where(Share.created_by_user_id == user.id)
    rows = db.execute(q).scalars().all()
    # Bulk-resolve usernames so we don't hit users N times for N shares.
    owner_ids = {s.created_by_user_id for s in rows if s.created_by_user_id is not None}
    usernames: dict[int, str] = {}
    if owner_ids:
        for uid, uname in db.execute(
            select(User.id, User.username).where(User.id.in_(owner_ids))
        ).all():
            usernames[uid] = uname
    out: list[ShareOut] = []
    for s in rows:
        count = len(_share_photos(s, db))
        fcount = len(_share_files(s, db))
        out.append(_to_share_out(s, count, usernames.get(s.created_by_user_id), file_count=fcount))
    return out


# ---------- Server-paginated list + minimap histogram ----------

class ShareListPage(BaseModel):
    total: int             # rows matching the current filter
    page: int
    page_size: int
    items: list[ShareOut]
    # Count of revoked / expired / quota-exhausted shares regardless of
    # the current status filter — drives the toolbar's "비활성 일괄
    # 정리 (N)" button enable/disable + label.
    inactive_count: int


class ShareMonthBucket(BaseModel):
    label: str             # "YYYY-MM"
    count: int


_VALID_SHARE_SORT = {"created_at", "expires_at", "views", "downloads", "status"}


def _share_sort_col(name: str, now: datetime):
    """Map a sort key from the UI onto a SQLA expression. `status` is a
    derived value (composed from revoked_at / expires_at / max_downloads),
    so we materialise it as a CASE that ranks active=0 → 한도소진=1 →
    만료=2 → 취소됨=3 (matches the badge colour spectrum)."""
    if name == "created_at":
        return Share.created_at
    if name == "expires_at":
        return Share.expires_at
    if name == "views":
        return Share.view_count
    if name == "downloads":
        return Share.download_count
    if name == "status":
        return case(
            (Share.revoked_at.is_not(None), 3),
            (and_(Share.expires_at.is_not(None), Share.expires_at < now), 2),
            (
                and_(
                    Share.max_downloads.is_not(None),
                    Share.download_count >= Share.max_downloads,
                ),
                1,
            ),
            else_=0,
        )
    return Share.created_at  # safety net for an unknown key


def _share_status_where(label: str, now: datetime):
    """Translate one of the UI's status labels into a WHERE clause.
    Returns None for an unknown label so the caller can 400 cleanly."""
    if label == "취소됨":
        return Share.revoked_at.is_not(None)
    if label == "만료됨":
        return and_(
            Share.revoked_at.is_(None),
            Share.expires_at.is_not(None),
            Share.expires_at < now,
        )
    if label == "한도소진":
        return and_(
            Share.revoked_at.is_(None),
            or_(Share.expires_at.is_(None), Share.expires_at >= now),
            Share.max_downloads.is_not(None),
            Share.download_count >= Share.max_downloads,
        )
    if label == "활성":
        return and_(
            Share.revoked_at.is_(None),
            or_(Share.expires_at.is_(None), Share.expires_at >= now),
            or_(
                Share.max_downloads.is_(None),
                Share.download_count < Share.max_downloads,
            ),
        )
    return None


def _share_inactive_where(now: datetime):
    return or_(
        Share.revoked_at.is_not(None),
        and_(Share.expires_at.is_not(None), Share.expires_at < now),
        and_(
            Share.max_downloads.is_not(None),
            Share.download_count >= Share.max_downloads,
        ),
    )


@admin_router.get("/page", response_model=ShareListPage)
def list_shares_page(
    page: int = Query(1, ge=1),
    page_size: int = Query(100, ge=1, le=500),
    status_filter: Optional[str] = Query(
        None, alias="status",
        description="활성 / 만료됨 / 취소됨 / 한도소진 — UI 라벨 그대로",
    ),
    sort: str = Query("created_at"),
    dir: str = Query("desc", pattern="^(asc|desc)$"),
    db: Session = Depends(get_db),
    user: User = Depends(require_auth),
) -> ShareListPage:
    """Server-paginated share list. Drives the infinite-scroll admin UI
    so the table stays responsive past a few thousand rows; client-side
    filter/sort would otherwise need every share fetched up front."""
    now = datetime.utcnow()
    base = select(Share)
    if not user.is_admin:
        base = base.where(Share.created_by_user_id == user.id)

    if status_filter:
        where = _share_status_where(status_filter, now)
        if where is None:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                f"알 수 없는 상태 필터: {status_filter!r}",
            )
        base = base.where(where)

    total = int(db.execute(
        select(func.count()).select_from(base.subquery())
    ).scalar_one() or 0)

    # Explicit Share.id count + same WHERE clause as the purge endpoint
    # uses. Using a subquery wrapper would lose the index-friendly
    # OR-of-(IS NOT NULL / range / cap) pattern; this stays a single
    # plain count query.
    inactive_q = select(func.count(Share.id)).where(_share_inactive_where(now))
    if not user.is_admin:
        inactive_q = inactive_q.where(Share.created_by_user_id == user.id)
    inactive_count = int(db.execute(inactive_q).scalar_one() or 0)

    if sort not in _VALID_SHARE_SORT:
        sort = "created_at"
    sort_col = _share_sort_col(sort, now)
    # Always tack id as a stable secondary so paging is deterministic
    # even when the primary key has duplicates / nulls.
    order_clauses = [
        sort_col.desc() if dir == "desc" else sort_col.asc(),
        Share.id.desc(),
    ]
    rows = db.execute(
        base.order_by(*order_clauses)
        .offset((page - 1) * page_size)
        .limit(page_size)
    ).scalars().all()

    owner_ids = {s.created_by_user_id for s in rows if s.created_by_user_id is not None}
    usernames: dict[int, str] = {}
    if owner_ids:
        for uid, uname in db.execute(
            select(User.id, User.username).where(User.id.in_(owner_ids))
        ).all():
            usernames[uid] = uname
    items: list[ShareOut] = []
    for s in rows:
        count = len(_share_photos(s, db))
        fcount = len(_share_files(s, db))
        items.append(_to_share_out(s, count, usernames.get(s.created_by_user_id), file_count=fcount))
    return ShareListPage(
        total=total,
        page=page,
        page_size=page_size,
        items=items,
        inactive_count=inactive_count,
    )


@admin_router.get("/month-histogram", response_model=list[ShareMonthBucket])
def share_month_histogram(
    status_filter: Optional[str] = Query(None, alias="status"),
    sort: str = Query("created_at"),
    dir: str = Query("desc", pattern="^(asc|desc)$"),
    db: Session = Depends(get_db),
    user: User = Depends(require_auth),
) -> list[ShareMonthBucket]:
    """RLE month-bucket count for the share list in the same order
    /page uses. The minimap consumes this so its labels track whatever
    filter + sort the admin currently has applied."""
    now = datetime.utcnow()
    base = select(Share.created_at, Share.id)
    if not user.is_admin:
        base = base.where(Share.created_by_user_id == user.id)
    if status_filter:
        where = _share_status_where(status_filter, now)
        if where is None:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                f"알 수 없는 상태 필터: {status_filter!r}",
            )
        base = base.where(where)
    if sort not in _VALID_SHARE_SORT:
        sort = "created_at"
    sort_col = _share_sort_col(sort, now)
    base = base.order_by(
        sort_col.desc() if dir == "desc" else sort_col.asc(),
        Share.id.desc(),
    )
    rows = db.execute(base).all()

    out: list[ShareMonthBucket] = []
    cur_label: Optional[str] = None
    cnt = 0
    for r in rows:
        ca = r[0]
        label = f"{ca.year:04d}-{ca.month:02d}" if ca is not None else ""
        if label == cur_label:
            cnt += 1
        else:
            if cur_label is not None:
                out.append(ShareMonthBucket(label=cur_label, count=cnt))
            cur_label = label
            cnt = 1
    if cur_label is not None and cnt > 0:
        out.append(ShareMonthBucket(label=cur_label, count=cnt))
    return out


def _create_share_for_ids(
    db: Session,
    user: User,
    ids: list[int],
    *,
    title: Optional[str],
    password: Optional[str],
    expires_in_days: Optional[int],
    max_downloads: Optional[int],
    strip_exif: bool,
    audit_source: str = "ids",
) -> ShareOut:
    """Shared core for /shares (photo-id list) and /shares/from-folder
    (folder-scoped scan). Caller provides the pre-deduped ids list in
    the order the share should display.
    """
    if not ids:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, "photo_ids가 비어 있습니다"
        )

    # ACL guard — the share creator must have at least read access on
    # every photo. Hidden roots → 404 (caller doesn't even know the
    # photo exists). Once a share is created its viewers bypass ACL
    # via the token, so this is the only check point.
    require_photo_ids_level(db, user, ids, "read")

    expires_at = None
    if expires_in_days is not None and expires_in_days > 0:
        expires_at = datetime.utcnow() + timedelta(days=expires_in_days)

    s = Share(
        token=_new_token(),
        photo_id=ids[0],  # legacy column gets the first photo for back-compat
        title=title,
        password_hash=hash_password(password) if password else None,
        expires_at=expires_at,
        max_downloads=max_downloads,
        created_by_user_id=user.id,
        strip_exif=bool(strip_exif),
    )
    db.add(s)
    db.flush()
    for idx, pid in enumerate(ids):
        db.add(ShareItem(share_id=s.id, photo_id=pid, sort_idx=idx))
    audit.record(
        db, user, "share.create", "share", s.id,
        detail={"photo_count": len(ids), "title": title,
                "password": bool(password),
                "expires_in_days": expires_in_days,
                "max_downloads": max_downloads,
                "source": audit_source},
    )
    db.commit()
    db.refresh(s)
    return _to_share_out(s, len(ids))


@admin_router.post("", response_model=ShareOut)
def create_share(
    payload: ShareCreateIn,
    db: Session = Depends(get_db),
    user: User = Depends(require_can_share),
) -> ShareOut:
    # Normalise input into a unique photo-id list, preserving the
    # caller-supplied order so spider-view / album-view ordering is honoured.
    ids: list[int] = []
    if payload.photo_ids:
        for pid in payload.photo_ids:
            if pid not in ids:
                ids.append(int(pid))
    if not ids and payload.photo_id is not None:
        ids = [int(payload.photo_id)]
    if not ids:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, "photo_ids가 비어 있습니다"
        )
    existing = db.execute(
        select(Photo.id).where(Photo.id.in_(ids))
    ).scalars().all()
    existing_set = set(existing)
    missing = [pid for pid in ids if pid not in existing_set]
    if missing:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, f"존재하지 않는 사진 id: {missing[:5]}..."
        )

    return _create_share_for_ids(
        db, user, ids,
        title=payload.title,
        password=payload.password,
        expires_in_days=payload.expires_in_days,
        max_downloads=payload.max_downloads,
        strip_exif=bool(payload.strip_exif),
        audit_source="ids",
    )


class FileShareCreateIn(BaseModel):
    """Create a public share for one or more files (kind='file' roots)."""
    file_ids: list[int]
    password: Optional[str] = None
    expires_in_days: Optional[int] = Field(default=None, ge=0, le=3650)
    max_downloads: Optional[int] = Field(default=None, ge=1, le=10000)
    title: Optional[str] = None


@admin_router.post("/files", response_model=ShareOut)
def create_file_share(
    payload: FileShareCreateIn,
    db: Session = Depends(get_db),
    user: User = Depends(require_can_share),
) -> ShareOut:
    # Preserve caller order, dedupe.
    ids: list[int] = []
    for fid in payload.file_ids:
        if fid not in ids:
            ids.append(int(fid))
    if not ids:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "file_ids가 비어 있습니다")

    files = db.execute(
        select(File).where(File.id.in_(ids), File.status == "active")
    ).scalars().all()
    fmap = {f.id: f for f in files}
    missing = [i for i in ids if i not in fmap]
    if missing:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, f"존재하지 않는 파일 id: {missing[:5]}...")
    # ACL: creator needs read access on each file's containing folder.
    for f in files:
        parent = f.rel_path.rsplit("/", 1)[0] if "/" in f.rel_path else ""
        if effective_folder_level(db, user, f.root_id, parent) == "hidden":
            raise HTTPException(status.HTTP_404_NOT_FOUND)

    expires_at = None
    if payload.expires_in_days is not None and payload.expires_in_days > 0:
        expires_at = datetime.utcnow() + timedelta(days=payload.expires_in_days)
    s = Share(
        token=_new_token(),
        photo_id=None,                       # file share — no legacy photo
        title=payload.title,
        password_hash=hash_password(payload.password) if payload.password else None,
        expires_at=expires_at,
        max_downloads=payload.max_downloads,
        created_by_user_id=user.id,
        strip_exif=False,
    )
    db.add(s)
    db.flush()
    for idx, fid in enumerate(ids):
        db.add(ShareFileItem(share_id=s.id, file_id=fid, sort_idx=idx))
    audit.record(
        db, user, "share.create", "share", s.id,
        detail={"file_count": len(ids), "kind": "file", "title": payload.title,
                "password": bool(payload.password),
                "expires_in_days": payload.expires_in_days,
                "max_downloads": payload.max_downloads},
    )
    db.commit()
    db.refresh(s)
    return _to_share_out(s, 0, file_count=len(ids))


class FolderShareCreateIn(BaseModel):
    """Same options as ShareCreateIn but the photo set is sourced
    from a (root_id, path_prefix) filter instead of a literal id
    list — used by the folder-tree right-click "공유하기" menu."""

    root_id: int
    path_prefix: str = ""        # "" = entire root
    password: Optional[str] = None
    expires_in_days: Optional[int] = Field(default=None, ge=0, le=3650)
    max_downloads: Optional[int] = Field(default=None, ge=1, le=10000)
    title: Optional[str] = None
    strip_exif: Optional[bool] = False


@admin_router.post("/from-folder", response_model=ShareOut)
def create_share_from_folder(
    payload: FolderShareCreateIn,
    db: Session = Depends(get_db),
    user: User = Depends(require_can_share),
) -> ShareOut:
    """Create a share for every active photo under (root_id, path_prefix).
    Walks the folder server-side so the client doesn't have to fetch
    a potentially huge photo-id list first.

    Ordering matches the gallery's default — newest taken_at first,
    then id desc — so the share's spider view reads chronologically
    the same way the folder does.
    """
    prefix = (payload.path_prefix or "").strip()
    q = select(Photo.id).where(
        Photo.status == "active",
        Photo.root_id == int(payload.root_id),
    )
    if prefix:
        # rel_path is normalised POSIX in the DB. Match anything under
        # this folder by LIKE 'prefix%'; trailing '/' on the prefix
        # prevents "foo/bar" from also matching "foo/barbaz".
        if not prefix.endswith("/"):
            prefix = prefix + "/"
        q = q.where(Photo.rel_path.like(prefix + "%"))
    q = q.order_by(
        Photo.taken_at.desc().nullslast(),
        Photo.id.desc(),
    )
    ids = [int(pid) for pid in db.execute(q).scalars().all()]
    if not ids:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, "폴더에 사진이 없습니다"
        )
    return _create_share_for_ids(
        db, user, ids,
        title=payload.title,
        password=payload.password,
        expires_in_days=payload.expires_in_days,
        max_downloads=payload.max_downloads,
        strip_exif=bool(payload.strip_exif),
        audit_source=f"folder:{payload.root_id}:{payload.path_prefix or '/'}",
    )


class SharePatchIn(BaseModel):
    """Partial update — only the fields actually present in the request
    are applied. `password=null` clears, any string sets; `expires_at=null`
    clears the expiry; `max_downloads=null` lifts the cap."""

    title: Optional[str] = None
    password: Optional[str] = None
    expires_at: Optional[datetime] = None
    max_downloads: Optional[int] = None
    strip_exif: Optional[bool] = None


@admin_router.patch("/{share_id}", response_model=ShareOut)
def update_share(
    share_id: int,
    payload: SharePatchIn,
    db: Session = Depends(get_db),
    user: User = Depends(require_auth),
) -> ShareOut:
    s = db.get(Share, share_id)
    if s is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    _require_share_owner_or_admin(s, user)
    if s.revoked_at is not None:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, "revoked share cannot be edited"
        )
    fields = payload.model_dump(exclude_unset=True)
    if "title" in fields:
        s.title = (fields["title"] or "").strip() or None
    if "password" in fields:
        pw = fields["password"]
        s.password_hash = hash_password(pw) if pw else None
    if "expires_at" in fields:
        s.expires_at = fields["expires_at"]
    if "max_downloads" in fields:
        mx = fields["max_downloads"]
        s.max_downloads = mx if (mx and mx > 0) else None
    if "strip_exif" in fields:
        s.strip_exif = bool(fields["strip_exif"])
    db.commit()
    db.refresh(s)
    return _to_share_out(s, len(_share_photos(s, db)),
                         file_count=len(_share_files(s, db)))


class PurgeInactiveOut(BaseModel):
    revoked: int
    expired: int
    cap_reached: int
    total: int


@admin_router.post("/purge-inactive", response_model=PurgeInactiveOut)
def purge_inactive(db: Session = Depends(get_db)) -> PurgeInactiveOut:
    """Hard-delete every share that's no longer usable: revoked,
    past its expiry, or hit its download cap. share_items rows go
    away via the FK cascade. Active shares are untouched.

    The three bucket counts in the response let the admin UI show
    "x revoked / y expired / z cap-reached, n total purged" toast.
    """
    now = datetime.utcnow()
    rows = db.execute(
        select(Share).where(
            or_(
                Share.revoked_at.is_not(None),
                and_(Share.expires_at.is_not(None), Share.expires_at < now),
                and_(
                    Share.max_downloads.is_not(None),
                    Share.download_count >= Share.max_downloads,
                ),
            )
        )
    ).scalars().all()
    revoked_n = expired_n = cap_n = 0
    for s in rows:
        if s.revoked_at is not None:
            revoked_n += 1
        elif s.expires_at is not None and s.expires_at < now:
            expired_n += 1
        else:
            cap_n += 1
        db.delete(s)
    if rows:
        db.commit()
    return PurgeInactiveOut(
        revoked=revoked_n,
        expired=expired_n,
        cap_reached=cap_n,
        total=len(rows),
    )


@admin_router.delete("/{share_id}")
def revoke_share(
    share_id: int,
    hard: bool = False,
    db: Session = Depends(get_db),
    user: User = Depends(require_auth),
) -> dict:
    """Soft-revoke by default (sets revoked_at; the row stays for
    audit and can't be unrevoked). With ?hard=true the row is
    actually removed and the share_items FK cascade cleans up the
    photo associations — used by the admin UI to purge already-
    revoked shares from the list.
    """
    s = db.get(Share, share_id)
    if s is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    _require_share_owner_or_admin(s, user)
    if hard:
        audit.record(
            db, user, "share.purge", "share", s.id,
            detail={"token": s.token[:8] + "…", "title": s.title},
        )
        db.delete(s)
        db.commit()
        return {"ok": True, "purged": True}
    if s.revoked_at is None:
        s.revoked_at = datetime.utcnow()
        audit.record(
            db, user, "share.revoke", "share", s.id,
            detail={"token": s.token[:8] + "…", "title": s.title},
        )
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
    photos = _share_photos(s, db) if unlocked else []
    files = _share_files(s, db) if unlocked else []
    out = PublicShareOut(
        token=s.token,
        title=s.title,
        needs_password=(s.password_hash is not None) and not unlocked,
        expires_at=s.expires_at,
        photo_count=len(photos) if unlocked else 0,
        photos=[
            PublicPhotoInfo(
                id=p.id,
                filename=p.filename,
                taken_at=p.taken_at,
                camera_model=p.camera_model,
                width=p.width,
                height=p.height,
                media_kind=p.media_kind,
            )
            for p in photos
        ],
        file_count=len(files) if unlocked else 0,
        files=[
            PublicFileInfo(
                id=f.id, filename=f.filename, ext=f.ext,
                mime=f.mime, size=f.file_size,
            )
            for f in files
        ],
    )
    if unlocked:
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
        if len(unlocked) > 50:
            unlocked = unlocked[-50:]
        request.session[SESSION_UNLOCKED] = unlocked
    return {"ok": True}


@public_router.get("/{token}/thumb/{photo_id}")
def get_share_thumb(
    token: str,
    photo_id: int,
    request: Request,
    size: int = Query(256),
    db: Session = Depends(get_db),
) -> FileResponse:
    s = _resolve(token, db)
    if not _is_unlocked(s, request):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "암호 필요")
    p = _verify_photo_in_share(s, photo_id, db)
    return FileResponse(_thumb_file(p, size), media_type="image/jpeg")


@public_router.get("/{token}/original/{photo_id}")
def get_share_original(
    token: str,
    photo_id: int,
    request: Request,
    db: Session = Depends(get_db),
) -> Response:
    s = _resolve(token, db)
    if not _is_unlocked(s, request):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "암호 필요")
    p = _verify_photo_in_share(s, photo_id, db)
    root = db.get(Root, p.root_id)
    if root is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "root missing")
    src = Path(join_root(root.abs_path, p.rel_path))
    if not src.exists():
        raise HTTPException(status.HTTP_404_NOT_FOUND, "원본 파일이 사라졌습니다")
    # Reserve the quota slot atomically BEFORE returning the file so
    # concurrent requests can't both pass a non-atomic check and both
    # exceed max_downloads.
    _atomic_consume_download(s, db)
    stripped = _maybe_strip_response(s, p, src)
    if stripped is not None:
        return stripped
    return FileResponse(src, filename=p.filename)


@public_router.get("/{token}/file/{file_id}")
def get_share_file(
    token: str,
    file_id: int,
    request: Request,
    db: Session = Depends(get_db),
) -> Response:
    """Download a shared file (files domain). Same active/unlock/quota
    rules as photo originals; no EXIF stripping (not media)."""
    s = _resolve(token, db)
    if not _is_unlocked(s, request):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "암호 필요")
    f = _verify_file_in_share(s, file_id, db)
    root = db.get(Root, f.root_id)
    if root is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "root missing")
    src = Path(join_root(root.abs_path, f.rel_path))
    if not src.exists():
        raise HTTPException(status.HTTP_404_NOT_FOUND, "원본 파일이 사라졌습니다")
    _atomic_consume_download(s, db)
    return FileResponse(
        src, filename=f.filename, media_type=f.mime or "application/octet-stream")


@public_router.get("/{token}/video/{photo_id}")
def get_share_video(
    token: str,
    photo_id: int,
    request: Request,
    db: Session = Depends(get_db),
) -> Response:
    """Inline video playback for the share viewer — serves the H.264 proxy
    when ready, else the original (Range-capable for seeking). This is a
    *preview*, not a download, so it does NOT consume the share's download
    quota (a player issues many Range requests)."""
    import mimetypes

    s = _resolve(token, db)
    if not _is_unlocked(s, request):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "암호 필요")
    p = _verify_photo_in_share(s, photo_id, db)
    if p.media_kind == "video" and p.proxy_status == "done" and p.sha256:
        from .worker.transcode import proxy_path
        pp = proxy_path(p.sha256)
        if pp.exists():
            try:
                pp.touch()   # LRU bump
            except OSError:
                pass
            return FileResponse(pp, media_type="video/mp4")
    root = db.get(Root, p.root_id)
    if root is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "root missing")
    src = Path(join_root(root.abs_path, p.rel_path))
    if not src.exists():
        raise HTTPException(status.HTTP_404_NOT_FOUND, "원본 파일이 사라졌습니다")
    media_type = mimetypes.guess_type(p.filename)[0] or "application/octet-stream"
    return FileResponse(src, media_type=media_type)


@public_router.post("/{token}/proxy/{photo_id}")
def request_share_proxy(
    token: str,
    photo_id: int,
    request: Request,
    db: Session = Depends(get_db),
) -> dict:
    """Build a web-playable H.264 proxy for a shared video that the browser
    can't decode. Bounded to photos actually in this share, then polled by
    the viewer until {status: 'done'}."""
    s = _resolve(token, db)
    if not _is_unlocked(s, request):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "암호 필요")
    p = _verify_photo_in_share(s, photo_id, db)
    if p.media_kind != "video":
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "not a video")
    if p.proxy_status == "done" and p.sha256:
        from .worker.transcode import proxy_path
        if proxy_path(p.sha256).exists():
            return {"status": "done"}
    if not p.sha256:
        return {"status": p.proxy_status or "pending"}
    from .worker import photo_work as photo_work_mod
    if p.proxy_status in (None, "pending"):
        photo_work_mod.enqueue_stage(
            db, photo_id=photo_id, stage="transcode", priority=5,
        )
        p.proxy_status = "pending"
        p.proxy_error = None
        db.commit()
    return {"status": p.proxy_status or "pending", "error": p.proxy_error}


# Legacy: callers that don't supply photo_id default to the first photo
# in the share (matches the old single-photo URL shape).
@public_router.get("/{token}/thumb")
def get_share_thumb_legacy(
    token: str,
    request: Request,
    size: int = Query(256),
    db: Session = Depends(get_db),
) -> FileResponse:
    s = _resolve(token, db)
    if not _is_unlocked(s, request):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "암호 필요")
    photos = _share_photos(s, db)
    if not photos:
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    return FileResponse(_thumb_file(photos[0], size), media_type="image/jpeg")


@public_router.get("/{token}/original")
def get_share_original_legacy(
    token: str, request: Request, db: Session = Depends(get_db)
) -> Response:
    s = _resolve(token, db)
    if not _is_unlocked(s, request):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "암호 필요")
    photos = _share_photos(s, db)
    if not photos:
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    p = photos[0]
    root = db.get(Root, p.root_id)
    if root is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "root missing")
    src = Path(join_root(root.abs_path, p.rel_path))
    if not src.exists():
        raise HTTPException(status.HTTP_404_NOT_FOUND, "원본 파일이 사라졌습니다")
    _atomic_consume_download(s, db)
    stripped = _maybe_strip_response(s, p, src)
    if stripped is not None:
        return stripped
    return FileResponse(src, filename=p.filename)


@public_router.get("/{token}/zip")
def get_share_zip(
    token: str, request: Request, db: Session = Depends(get_db)
) -> StreamingResponse:
    """Bundle every photo in the share into a ZIP and stream it.

    Uses stream-zip so bytes start flowing to the client as soon as
    the first file is read — no temp file, no full-archive memory
    buffer, no waiting for the whole bundle to build before the
    HTTP response starts. The previous tempfile + FileResponse path
    would hang for ~10 min on a 50 GB folder share, hitting reverse
    proxy timeouts and OOM-killing the worker on small NAS RAM.

    Counts as a single download for the share's max_downloads cap.
    """
    from datetime import datetime as _dt
    from stream_zip import ZIP_64, stream_zip       # type: ignore

    s = _resolve(token, db)
    if not _is_unlocked(s, request):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "암호 필요")
    photos = _share_photos(s, db)
    if not photos:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "공유에 사진이 없습니다")
    # Reserve the slot BEFORE streaming — without this, two
    # concurrent zip requests can both pass a non-atomic check and
    # both stream the bundle, blowing past a max_downloads=1 cap.
    _atomic_consume_download(s, db)

    # Eager-fetch what the generator needs before the request's db
    # session goes away. Generator runs in the response phase where
    # the Depends(get_db) session is already closed.
    seen: set[str] = set()
    members: list[tuple[str, Path, bool, str]] = []   # (arcname, src, strip_eligible, ext_l)
    strip_exif = bool(getattr(s, "strip_exif", False))
    for p in photos:
        root = db.get(Root, p.root_id)
        if root is None:
            continue
        src = Path(join_root(root.abs_path, p.rel_path))
        if not src.exists():
            continue
        arcname = p.filename
        if arcname in seen:
            stem, dot, ext = arcname.rpartition(".")
            arcname = f"{stem or arcname}_{p.id}{dot}{ext}"
        seen.add(arcname)
        ext_l = (p.filename.rsplit(".", 1)[-1]
                 if "." in p.filename else "").lower()
        is_jpeg = ext_l in ("jpg", "jpeg")
        members.append((arcname, src, strip_exif and is_jpeg, ext_l))

    if not members:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "원본 파일이 모두 사라졌습니다")

    now = _dt.utcnow()
    mode = 0o600
    CHUNK = 1024 * 1024

    def _file_chunks(path: Path):
        """Yield 1 MB chunks from a file without holding it in memory."""
        with open(path, "rb") as f:
            while True:
                buf = f.read(CHUNK)
                if not buf:
                    return
                yield buf

    def _stripped_chunks(path: Path):
        """GPS-strip path holds one file's bytes briefly in memory.
        Cheap for JPEGs (typical <50 MB) and the only way to feed
        modified bytes back into the zip stream."""
        stripped = _strip_gps_jpeg(path)
        if stripped is None:
            # Fall back to raw bytes if strip failed for any reason.
            yield from _file_chunks(path)
            return
        yield stripped

    def _zip_members():
        for arcname, src, do_strip, _ext in members:
            chunks = _stripped_chunks(src) if do_strip else _file_chunks(src)
            yield (arcname, now, mode, ZIP_64, chunks)

    fname = f"share-{token[:8]}.zip"
    return StreamingResponse(
        stream_zip(_zip_members()),
        media_type="application/zip",
        headers={
            "Content-Disposition": content_disposition("attachment", fname),
        },
    )



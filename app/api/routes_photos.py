"""Photo browsing API — list + thumbnail/original serving.

MVP 2 supports basic filtering (root, date range, status) and offset
pagination. Map/cluster endpoints land in a later MVP.
"""

from __future__ import annotations

import functools
import json
import logging
import mimetypes
import shutil
import subprocess
from datetime import datetime
from io import BytesIO
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..auth import require_admin, require_auth
from ..config import get_settings
from ..external import exiftool_path
from ..models import Photo, PhotoComment, PhotoLocation, PhotoRating, Root, User
from ..paths import TRASH_DIR
from ..scanner.utils import join_root
from ..worker.thumbs import RAW_EXTS, thumb_path
from .deps import get_db

log = logging.getLogger(__name__)

router = APIRouter(prefix="/photos", tags=["photos"])


class PhotoOut(BaseModel):
    id: int
    root_id: int
    rel_path: str
    filename: str
    media_kind: str
    ext: str
    sha256: str | None
    taken_at: datetime | None
    width: int | None
    height: int | None
    camera_model: str | None
    exif_status: str
    thumb_status: str
    status: str

    class Config:
        from_attributes = True


class PhotoPage(BaseModel):
    total: int
    page: int
    page_size: int
    items: list[PhotoOut]


def _apply_search_filters(
    q,
    db: Session,
    comment_q: str | None,
    min_rating: int | None,
    near_lat: float | None,
    near_lng: float | None,
    near_radius_deg: float | None,
):
    """Apply the comment / rating / place filters used by both list_photos
    and date_histogram so the gallery and the scroll indicator stay in sync.

    Wrapped in try/except for tables that may not exist on a pre-0004 DB —
    in that case the filter silently no-ops rather than 500'ing.
    """
    if comment_q:
        needle = f"%{comment_q.strip()}%"
        try:
            sub = (
                select(PhotoComment.photo_id)
                .where(PhotoComment.body.like(needle))
                .distinct()
            )
            q = q.where(Photo.id.in_(sub))
        except Exception:
            pass
    if min_rating is not None:
        try:
            sub = (
                select(PhotoRating.photo_id)
                .where(PhotoRating.rating >= min_rating)
                .distinct()
            )
            q = q.where(Photo.id.in_(sub))
        except Exception:
            pass
    if near_lat is not None and near_lng is not None:
        radius = near_radius_deg if near_radius_deg is not None else 0.05
        sub = (
            select(PhotoLocation.photo_id)
            .where(
                PhotoLocation.latitude.between(near_lat - radius, near_lat + radius),
                PhotoLocation.longitude.between(near_lng - radius, near_lng + radius),
            )
            .distinct()
        )
        q = q.where(Photo.id.in_(sub))
    return q


@router.get("", response_model=PhotoPage)
def list_photos(
    root_id: int | None = None,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
    status_filter: str = "active",
    comment_q: str | None = Query(None, description="comment substring (case-insensitive for ASCII)"),
    min_rating: int | None = Query(None, ge=1, le=5, description="any user's rating ≥ this"),
    near_lat: float | None = Query(None, ge=-90, le=90),
    near_lng: float | None = Query(None, ge=-180, le=180),
    near_radius_deg: float | None = Query(None, gt=0, le=10),
    page: int = Query(1, ge=1),
    page_size: int = Query(60, ge=1, le=500),
    db: Session = Depends(get_db),
) -> PhotoPage:
    q = select(Photo)
    if status_filter:
        q = q.where(Photo.status == status_filter)
    if root_id is not None:
        q = q.where(Photo.root_id == root_id)
    if date_from is not None:
        q = q.where(Photo.taken_at >= date_from)
    if date_to is not None:
        q = q.where(Photo.taken_at <= date_to)
    q = _apply_search_filters(
        q, db, comment_q, min_rating, near_lat, near_lng, near_radius_deg
    )

    total = db.execute(select(func.count()).select_from(q.subquery())).scalar_one()
    rows = db.execute(
        q.order_by(Photo.taken_at.desc().nullslast(), Photo.id.desc())
         .offset((page - 1) * page_size)
         .limit(page_size)
    ).scalars().all()
    return PhotoPage(
        total=total,
        page=page,
        page_size=page_size,
        items=[PhotoOut.model_validate(r) for r in rows],
    )


class MarkerOut(BaseModel):
    id: int
    lat: float
    lng: float


_NOMINATIM_UA = "MyPhotos (self-hosted photo catalog)"


@functools.lru_cache(maxsize=256)
def _nominatim_search(q: str, lang: str, limit: int) -> str:
    """Cached round-trip to OSM Nominatim. Returns raw JSON text."""
    import urllib.parse
    import urllib.request

    params = urllib.parse.urlencode(
        {"q": q, "format": "json", "limit": limit, "accept-language": lang}
    )
    url = f"https://nominatim.openstreetmap.org/search?{params}"
    req = urllib.request.Request(url, headers={"User-Agent": _NOMINATIM_UA})
    with urllib.request.urlopen(req, timeout=10) as resp:
        return resp.read().decode("utf-8")


class GeocodeHit(BaseModel):
    lat: float
    lng: float
    display_name: str
    type: str | None = None
    importance: float = 0.0


@router.get("/geocode", response_model=list[GeocodeHit])
def geocode(
    q: str = Query(..., min_length=1, max_length=200),
    lang: str = Query("ko", max_length=8),
    limit: int = Query(5, ge=1, le=10),
) -> list[GeocodeHit]:
    """Resolve a place name to coordinates via OSM Nominatim.

    Used by the header search: the frontend picks the top hit and uses
    its lat/lng to filter the timeline / histogram via near_lat / near_lng.
    Lookups are cached in-process so repeating the same query is free
    (and stays inside Nominatim's usage policy).
    """
    import json

    q = (q or "").strip()
    if not q:
        return []
    try:
        raw = _nominatim_search(q, lang, limit)
        data = json.loads(raw)
    except Exception as e:
        log.warning("nominatim lookup failed: %s", e)
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, f"지오코드 실패: {e}"
        )
    return [
        GeocodeHit(
            lat=float(d["lat"]),
            lng=float(d["lon"]),
            display_name=d.get("display_name", ""),
            type=d.get("type"),
            importance=float(d.get("importance", 0)),
        )
        for d in data
    ]


@router.get("/nearby", response_model=list[PhotoOut])
def list_nearby(
    photo_id: int = Query(..., description="anchor photo id"),
    radius_deg: float = Query(0.005, gt=0, le=1.0,
                              description="lat/lng degrees (0.005 ≈ ~500m)"),
    limit: int = Query(100, ge=1, le=500),
    db: Session = Depends(get_db),
) -> list[PhotoOut]:
    """Photos taken within `radius_deg` of the anchor photo's GPS coordinates.

    Powers the map → lightbox flow: clicking a marker opens the lightbox
    over this set, so prev/next and the filmstrip surface neighboring
    photos taken at the same location. Result is ordered by taken_at desc
    (matches the timeline order), the anchor is included.
    """
    anchor = db.get(Photo, photo_id)
    if anchor is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "anchor photo not found")
    loc = db.get(PhotoLocation, photo_id)
    if loc is None:
        # No GPS on the anchor — just hand back the photo by itself.
        return [PhotoOut.model_validate(anchor)]

    lat_min, lat_max = loc.latitude - radius_deg, loc.latitude + radius_deg
    lng_min, lng_max = loc.longitude - radius_deg, loc.longitude + radius_deg

    q = (
        select(Photo)
        .join(PhotoLocation, Photo.id == PhotoLocation.photo_id)
        .where(
            Photo.status == "active",
            Photo.thumb_status.in_(("ok", "partial")),
            PhotoLocation.latitude.between(lat_min, lat_max),
            PhotoLocation.longitude.between(lng_min, lng_max),
        )
        .order_by(Photo.taken_at.desc().nullslast(), Photo.id.desc())
        .limit(limit)
    )
    rows = db.execute(q).scalars().all()
    return [PhotoOut.model_validate(r) for r in rows]


class YearBucket(BaseModel):
    """One row in the timeline date histogram. `year=None` = photos without `taken_at`."""

    year: int | None
    count: int


@router.get("/date-histogram", response_model=list[YearBucket])
def date_histogram(
    comment_q: str | None = None,
    min_rating: int | None = Query(None, ge=1, le=5),
    near_lat: float | None = Query(None, ge=-90, le=90),
    near_lng: float | None = Query(None, ge=-180, le=180),
    near_radius_deg: float | None = Query(None, gt=0, le=10),
    db: Session = Depends(get_db),
) -> list[YearBucket]:
    """Year buckets across the active timeline (newest first, no-date last).

    Accepts the same search filters as list_photos so the right-side
    scrollbar represents the *filtered* range when a search is active.
    """
    q = (
        select(
            func.strftime("%Y", Photo.taken_at).label("year"),
            func.count().label("count"),
        )
        .where(Photo.status == "active")
        .group_by("year")
    )
    # The filter helper expects a SELECT on Photo, but for the histogram we
    # need to attach the same `Photo.id.in_(...)` predicates to a grouped
    # query. Inline them here rather than reshaping the helper.
    base_filters = (
        select(Photo.id).where(Photo.status == "active")
    )
    base_filters = _apply_search_filters(
        base_filters, db, comment_q, min_rating, near_lat, near_lng, near_radius_deg
    )
    if comment_q or min_rating is not None or (near_lat is not None and near_lng is not None):
        q = q.where(Photo.id.in_(base_filters))
    rows = db.execute(q).all()

    dated: list[YearBucket] = []
    no_date_count = 0
    for r in rows:
        if r.year is None:
            no_date_count = r.count
        else:
            dated.append(YearBucket(year=int(r.year), count=r.count))
    # Match the listing order (taken_at desc nullslast).
    dated.sort(key=lambda b: b.year or 0, reverse=True)
    if no_date_count:
        dated.append(YearBucket(year=None, count=no_date_count))
    return dated


@router.get("/locations", response_model=list[MarkerOut])
def list_locations(
    bbox: str | None = Query(
        None,
        description="Filter: 'minLng,minLat,maxLng,maxLat'. Omit to return everything.",
    ),
    limit: int = Query(5000, ge=1, le=50000),
    db: Session = Depends(get_db),
) -> list[MarkerOut]:
    """Lightweight marker list for the map view. Lat/Lng only.

    Filters out photos without a thumbnail — otherwise the popup would
    show a broken image and clicking it would 404 from the lightbox.
    """
    q = select(PhotoLocation.photo_id, PhotoLocation.latitude, PhotoLocation.longitude).join(
        Photo, Photo.id == PhotoLocation.photo_id
    ).where(
        Photo.status == "active",
        Photo.thumb_status.in_(("ok", "partial")),
    )

    if bbox:
        try:
            min_lng, min_lat, max_lng, max_lat = (float(x) for x in bbox.split(","))
        except ValueError as e:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, f"bad bbox: {e}")
        q = q.where(
            PhotoLocation.latitude.between(min_lat, max_lat),
            PhotoLocation.longitude.between(min_lng, max_lng),
        )

    rows = db.execute(q.limit(limit)).all()
    return [MarkerOut(id=r[0], lat=r[1], lng=r[2]) for r in rows]


class CommentOut(BaseModel):
    id: int
    photo_id: int
    user_id: int | None
    username: str | None
    body: str
    created_at: datetime
    updated_at: datetime
    can_edit: bool  # true if requester is the author or an admin


class PhotoDetail(PhotoOut):
    """Full per-photo info (extends PhotoOut with EXIF + GPS for the side panel)."""

    file_size: int | None = None
    mtime: datetime | None = None
    camera_make: str | None = None
    lens: str | None = None
    iso: int | None = None
    fnumber: float | None = None
    exposure: str | None = None
    focal_length: float | None = None
    orientation: int | None = None
    duration_seconds: float | None = None
    exif_extractor: str | None = None
    indexed_at: datetime
    updated_at: datetime
    # Joined from photo_locations (null if no GPS).
    latitude: float | None = None
    longitude: float | None = None
    altitude: float | None = None
    # Social (added in 0004 migration).
    rating_avg: float | None = None
    rating_count: int = 0
    my_rating: int | None = None
    comment_count: int = 0
    comments: list[CommentOut] = []


@router.get("/{photo_id}", response_model=PhotoOut)
def get_photo(photo_id: int, db: Session = Depends(get_db)) -> PhotoOut:
    p = db.get(Photo, photo_id)
    if p is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    return PhotoOut.model_validate(p)


@router.get("/{photo_id}/details", response_model=PhotoDetail)
def get_photo_details(
    photo_id: int,
    user: User = Depends(require_auth),
    db: Session = Depends(get_db),
) -> PhotoDetail:
    p = db.get(Photo, photo_id)
    if p is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    out = PhotoDetail.model_validate(p)
    loc = db.get(PhotoLocation, photo_id)
    if loc is not None:
        out.latitude = loc.latitude
        out.longitude = loc.longitude
        out.altitude = loc.altitude

    # Rating aggregates + my rating. Wrapped in try so /details still works
    # before the 0004 migration has been applied.
    try:
        agg = db.execute(
            select(func.avg(PhotoRating.rating), func.count(PhotoRating.user_id))
            .where(PhotoRating.photo_id == photo_id)
        ).one()
        out.rating_avg = float(agg[0]) if agg[0] is not None else None
        out.rating_count = int(agg[1] or 0)
        out.my_rating = db.execute(
            select(PhotoRating.rating).where(
                PhotoRating.photo_id == photo_id,
                PhotoRating.user_id == user.id,
            )
        ).scalar_one_or_none()
    except Exception:
        pass

    # Comments + count (also tolerant of unmigrated DB).
    try:
        rows = db.execute(
            select(PhotoComment, User.username)
            .outerjoin(User, User.id == PhotoComment.user_id)
            .where(PhotoComment.photo_id == photo_id)
            .order_by(PhotoComment.created_at.asc())
        ).all()
        out.comments = [
            CommentOut(
                id=c.id,
                photo_id=c.photo_id,
                user_id=c.user_id,
                username=uname,
                body=c.body,
                created_at=c.created_at,
                updated_at=c.updated_at,
                can_edit=(c.user_id == user.id) or user.is_admin,
            )
            for c, uname in rows
        ]
        out.comment_count = len(out.comments)
    except Exception:
        pass

    return out


# ---- ratings ----

class RatingIn(BaseModel):
    """`rating=None` clears the user's rating; otherwise must be 1–5."""

    rating: int | None = None


@router.put("/{photo_id}/rating")
def set_rating(
    photo_id: int,
    payload: RatingIn,
    user: User = Depends(require_auth),
    db: Session = Depends(get_db),
) -> dict:
    if db.get(Photo, photo_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    if payload.rating is not None and not (1 <= payload.rating <= 5):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "rating must be 1-5 or null")

    existing = db.execute(
        select(PhotoRating).where(
            PhotoRating.photo_id == photo_id, PhotoRating.user_id == user.id
        )
    ).scalar_one_or_none()

    if payload.rating is None:
        if existing is not None:
            db.delete(existing)
            db.commit()
        return {"ok": True, "my_rating": None}

    if existing is not None:
        existing.rating = payload.rating
    else:
        db.add(PhotoRating(
            photo_id=photo_id, user_id=user.id, rating=payload.rating
        ))
    db.commit()
    return {"ok": True, "my_rating": payload.rating}


# ---- comments ----

class CommentIn(BaseModel):
    body: str  # validated below — non-empty after strip, <= 2000 chars


def _comment_out(c: PhotoComment, username: str | None, requester: User) -> CommentOut:
    return CommentOut(
        id=c.id,
        photo_id=c.photo_id,
        user_id=c.user_id,
        username=username,
        body=c.body,
        created_at=c.created_at,
        updated_at=c.updated_at,
        can_edit=(c.user_id == requester.id) or requester.is_admin,
    )


@router.post("/{photo_id}/comments", response_model=CommentOut, status_code=status.HTTP_201_CREATED)
def add_comment(
    photo_id: int,
    payload: CommentIn,
    user: User = Depends(require_auth),
    db: Session = Depends(get_db),
) -> CommentOut:
    if db.get(Photo, photo_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    body = (payload.body or "").strip()
    if not body:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "댓글 내용이 비어있습니다")
    if len(body) > 2000:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "댓글은 2000자 이하만 가능합니다")
    c = PhotoComment(photo_id=photo_id, user_id=user.id, body=body)
    db.add(c)
    db.commit()
    db.refresh(c)
    return _comment_out(c, user.username, user)


@router.patch("/{photo_id}/comments/{comment_id}", response_model=CommentOut)
def edit_comment(
    photo_id: int,
    comment_id: int,
    payload: CommentIn,
    user: User = Depends(require_auth),
    db: Session = Depends(get_db),
) -> CommentOut:
    c = db.get(PhotoComment, comment_id)
    if c is None or c.photo_id != photo_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    if c.user_id != user.id and not user.is_admin:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "본인 댓글만 수정 가능")
    body = (payload.body or "").strip()
    if not body:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "댓글 내용이 비어있습니다")
    if len(body) > 2000:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "댓글은 2000자 이하만 가능합니다")
    c.body = body
    db.commit()
    db.refresh(c)
    uname = db.get(User, c.user_id).username if c.user_id else None
    return _comment_out(c, uname, user)


@router.delete("/{photo_id}/comments/{comment_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_comment(
    photo_id: int,
    comment_id: int,
    user: User = Depends(require_auth),
    db: Session = Depends(get_db),
) -> None:
    c = db.get(PhotoComment, comment_id)
    if c is None or c.photo_id != photo_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    if c.user_id != user.id and not user.is_admin:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "본인 댓글만 삭제 가능")
    db.delete(c)
    db.commit()


@router.get("/{photo_id}/thumb")
def get_thumb(
    photo_id: int,
    size: int = Query(256),
    db: Session = Depends(get_db),
) -> FileResponse:
    p = db.get(Photo, photo_id)
    if p is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    if not p.sha256:
        raise HTTPException(status.HTTP_409_CONFLICT, "photo not yet indexed")
    s = get_settings()
    sizes = sorted(s.thumbnails.sizes)
    # Prefer the smallest configured size that is >= the request. If that
    # one isn't on disk yet (e.g. larger size still pending while smaller
    # one finished), fall back to any size that actually exists — better
    # to serve a small thumb than 404.
    chosen = next((sz for sz in sizes if sz >= size), sizes[-1])
    path = thumb_path(p.sha256, chosen)
    if not path.exists():
        for sz in reversed(sizes):
            alt = thumb_path(p.sha256, sz)
            if alt.exists():
                path = alt
                break
        else:
            # DB says the thumb is ready but the file's gone — heal the
            # inconsistency so the worker re-generates next pass and the
            # photo eventually disappears from the map marker filter
            # (locations requires thumb_status in ok/partial).
            if p.thumb_status in ("ok", "partial"):
                p.thumb_status = "pending"
                p.thumb_error = "file missing on disk; requeued"
                try:
                    from ..worker.jobs import enqueue

                    enqueue(
                        db,
                        kind="index_file",
                        payload={"photo_id": p.id},
                        priority=5,
                    )
                    db.commit()
                except Exception:
                    db.rollback()
            raise HTTPException(
                status.HTTP_404_NOT_FOUND, "thumbnail not generated yet"
            )
    return FileResponse(path, media_type="image/jpeg")


@router.get("/{photo_id}/original")
def get_original(photo_id: int, db: Session = Depends(get_db)) -> FileResponse:
    """Serve the original file inline so browsers can display JPG/PNG/HEIC in a tab."""
    p = db.get(Photo, photo_id)
    if p is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    root = db.get(Root, p.root_id)
    if root is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "root missing")
    src = Path(join_root(root.abs_path, p.rel_path))
    if not src.exists():
        raise HTTPException(status.HTTP_404_NOT_FOUND, "file missing on disk")
    media_type = mimetypes.guess_type(p.filename)[0] or "application/octet-stream"
    # FileResponse with filename= forces attachment; build the disposition manually.
    return FileResponse(
        src,
        media_type=media_type,
        headers={
            "Content-Disposition": f'inline; filename="{p.filename}"',
        },
    )


# ---- Download (force attachment, optional PNG conversion) ----

_BROWSER_IMG_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp"}


def _read_image_any(src: Path, ext: str):
    """Return a PIL Image for any supported format (HEIC via pillow-heif,
    RAW via exiftool preview), or None if we can't decode."""
    from PIL import Image, UnidentifiedImageError

    try:
        from pillow_heif import register_heif_opener  # type: ignore

        register_heif_opener()
    except ImportError:
        pass

    raw_ext = ext.lower().lstrip(".")
    if raw_ext in RAW_EXTS:
        # Skip Pillow for RAW — go straight to exiftool preview.
        return _exiftool_preview_image(src)

    try:
        return Image.open(src)
    except (UnidentifiedImageError, OSError):
        return _exiftool_preview_image(src)


def _exiftool_preview_image(src: Path):
    """Pull the largest embedded JPEG preview via exiftool and return a PIL Image."""
    from PIL import Image, UnidentifiedImageError

    tool = exiftool_path()
    if not tool:
        return None
    for tag in ("-JpgFromRaw", "-PreviewImage", "-OtherImage", "-ThumbnailImage"):
        try:
            proc = subprocess.run(
                [tool, "-b", tag, str(src)],
                capture_output=True,
                timeout=30,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        if proc.returncode == 0 and proc.stdout and len(proc.stdout) > 1024:
            try:
                return Image.open(BytesIO(proc.stdout))
            except (UnidentifiedImageError, OSError):
                continue
    return None


@router.get("/{photo_id}/download")
def download_photo(
    photo_id: int,
    format: str = Query("original", pattern="^(original|png)$"),
    db: Session = Depends(get_db),
):
    """Force-download endpoint. `format=png` converts RAW/HEIC/etc. to PNG on the fly."""
    p = db.get(Photo, photo_id)
    if p is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    root = db.get(Root, p.root_id)
    if root is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "root missing")
    src = Path(join_root(root.abs_path, p.rel_path))
    if not src.exists():
        raise HTTPException(status.HTTP_404_NOT_FOUND, "file missing on disk")

    ext = (p.ext or "").lower()
    if not ext.startswith("."):
        ext = "." + ext

    if format == "original" or (ext in _BROWSER_IMG_EXTS and format == "png"):
        # No conversion: hand the file as an attachment.
        media_type = mimetypes.guess_type(p.filename)[0] or "application/octet-stream"
        return FileResponse(
            src,
            media_type=media_type,
            filename=p.filename,
        )

    # format == "png" for a non-JPG/PNG file → decode and re-encode.
    if p.media_kind != "image":
        raise HTTPException(
            status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, "동영상은 PNG 변환을 지원하지 않습니다"
        )

    from PIL import Image, ImageOps

    img = _read_image_any(src, ext)
    if img is None:
        raise HTTPException(
            status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            "이미지를 디코딩할 수 없습니다 (지원하지 않는 형식)",
        )
    try:
        img = ImageOps.exif_transpose(img)
        if img.mode != "RGB":
            img = img.convert("RGB")
        buf = BytesIO()
        img.save(buf, format="PNG", optimize=False)
    finally:
        try:
            img.close()
        except Exception:
            pass
    png_bytes = buf.getvalue()
    base = p.filename.rsplit(".", 1)[0] if "." in p.filename else p.filename
    out_name = f"{base}.png"
    return Response(
        content=png_bytes,
        media_type="image/png",
        headers={"Content-Disposition": f'attachment; filename="{out_name}"'},
    )


def _move_to_trash(p: Photo, root: Root, user: User | None) -> dict:
    """Move the original file from its root into data/trash/<photo_id>/.

    Writes a `_meta.json` sidecar with enough info to manually restore the
    file later. Returns a dict describing the outcome.
    """
    src = Path(join_root(root.abs_path, p.rel_path))
    if not src.exists():
        return {"moved": False, "reason": "source file already missing"}

    dest_dir = TRASH_DIR / str(p.id)
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / p.filename
    # Re-deletion (e.g. orphan row replayed) — don't clobber the prior copy.
    if dest.exists():
        ts = datetime.utcnow().strftime("%Y%m%d%H%M%S")
        dest = dest_dir / f"{ts}_{p.filename}"

    try:
        shutil.move(str(src), str(dest))
    except (OSError, shutil.Error) as e:
        log.warning("trash move failed for photo %s: %s", p.id, e)
        return {"moved": False, "reason": f"파일 이동 실패: {e}"}

    meta = {
        "photo_id": p.id,
        "original_root_id": p.root_id,
        "original_root_label": root.label,
        "original_root_abs_path": root.abs_path,
        "original_rel_path": p.rel_path,
        "filename": p.filename,
        "sha256": p.sha256,
        "file_size": p.file_size,
        "deleted_at": datetime.utcnow().isoformat(timespec="seconds") + "Z",
        "deleted_by": user.username if user else None,
        "trash_path": str(dest.relative_to(TRASH_DIR)),
    }
    try:
        (dest_dir / "_meta.json").write_text(
            json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except OSError as e:
        log.warning("trash meta write failed for photo %s: %s", p.id, e)
    return {"moved": True, "trash_path": str(dest)}


@router.delete("/{photo_id}")
def delete_photo(
    photo_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(require_admin),
) -> dict:
    """Move the original file to data/trash/ and mark the row as trashed.

    Admin-only — non-admin family members can browse and share but can't
    remove photos from the catalog. The DB row stays so the deletion is
    recoverable: restoring is a matter of moving the file back and
    flipping status to 'active'.
    """
    p = db.get(Photo, photo_id)
    if p is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    root = db.get(Root, p.root_id)
    if root is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "root missing")

    result = _move_to_trash(p, root, user)
    if p.status != "trashed":
        p.status = "trashed"
        db.commit()
    return {
        "ok": True,
        "id": photo_id,
        "status": p.status,
        "file_moved": result.get("moved", False),
        "reason": result.get("reason"),
    }

"""Photo browsing API — list + thumbnail/original serving.

MVP 2 supports basic filtering (root, date range, status) and offset
pagination. Map/cluster endpoints land in a later MVP.
"""

from __future__ import annotations

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
from ..models import Photo, PhotoLocation, Root, User
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


@router.get("", response_model=PhotoPage)
def list_photos(
    root_id: int | None = None,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
    status_filter: str = "active",
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
def date_histogram(db: Session = Depends(get_db)) -> list[YearBucket]:
    """Year buckets across the active timeline (newest first, no-date last).

    Lets the web viewer's right-side scrollbar render year tick marks and
    map a drag position to an absolute photo offset, so the user can jump
    across a multi-decade catalog in one motion.
    """
    rows = db.execute(
        select(
            func.strftime("%Y", Photo.taken_at).label("year"),
            func.count().label("count"),
        )
        .where(Photo.status == "active")
        .group_by("year")
    ).all()

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


@router.get("/{photo_id}", response_model=PhotoOut)
def get_photo(photo_id: int, db: Session = Depends(get_db)) -> PhotoOut:
    p = db.get(Photo, photo_id)
    if p is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    return PhotoOut.model_validate(p)


@router.get("/{photo_id}/details", response_model=PhotoDetail)
def get_photo_details(photo_id: int, db: Session = Depends(get_db)) -> PhotoDetail:
    p = db.get(Photo, photo_id)
    if p is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    out = PhotoDetail.model_validate(p)
    loc = db.get(PhotoLocation, photo_id)
    if loc is not None:
        out.latitude = loc.latitude
        out.longitude = loc.longitude
        out.altitude = loc.altitude
    return out


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

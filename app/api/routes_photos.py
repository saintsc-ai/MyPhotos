"""Photo browsing API — list + thumbnail/original serving.

MVP 2 supports basic filtering (root, date range, status) and offset
pagination. Map/cluster endpoints land in a later MVP.
"""

from __future__ import annotations

import functools
import json
import logging
import mimetypes
import re
import secrets
import shutil
import subprocess
import time
import zipfile
from datetime import datetime
from io import BytesIO
from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, status
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel
from sqlalchemy import func, or_, select, text
from sqlalchemy.orm import Session

from ..auth import require_admin, require_auth
from ..config import get_settings
from ..external import exiftool_path
from ..models import Photo, PhotoComment, PhotoLocation, PhotoRating, PhotoTag, Root, Tag, User
from ..paths import TMP_DIR, TRASH_DIR
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
    tag: str | None = None,
    tag_q: str | None = None,
    text_q: str | None = None,
    filename_q: str | None = None,
):
    """Apply the comment / rating / place / tag filters used by both
    list_photos and date_histogram so the gallery and the scroll indicator
    stay in sync.

    Wrapped in try/except for tables that may not exist on a pre-0004 DB —
    in that case the filter silently no-ops rather than 500'ing.
    """
    if text_q and text_q.strip():
        # Unified search: match a single needle against filename, rel_path,
        # description, comment body, and tag name — OR semantics across all
        # five fields. Each disjunct is either a direct column LIKE or an
        # in_(subquery), so SQLite can short-circuit per row.
        needle = f"%{text_q.strip()}%"
        n_lower = f"%{text_q.strip().lower()}%"
        conds = [
            func.lower(Photo.filename).like(n_lower),
            func.lower(Photo.rel_path).like(n_lower),
            func.lower(func.coalesce(Photo.description, "")).like(n_lower),
        ]
        if _has_table(db, "photo_comments"):
            conds.append(
                Photo.id.in_(
                    select(PhotoComment.photo_id).where(PhotoComment.body.like(needle))
                )
            )
        if _has_table(db, "tags"):
            conds.append(
                Photo.id.in_(
                    select(PhotoTag.photo_id)
                    .join(Tag, Tag.id == PhotoTag.tag_id)
                    .where(func.lower(Tag.name).like(n_lower))
                )
            )
        q = q.where(or_(*conds))
    if filename_q and filename_q.strip():
        # Filename-only search hits both `filename` and `rel_path` so users
        # can paste a path snippet ("2024/베트남") and find it.
        n_lower = f"%{filename_q.strip().lower()}%"
        q = q.where(
            or_(
                func.lower(Photo.filename).like(n_lower),
                func.lower(Photo.rel_path).like(n_lower),
            )
        )
    if comment_q and _has_table(db, "photo_comments"):
        needle = f"%{comment_q.strip()}%"
        sub = (
            select(PhotoComment.photo_id)
            .where(PhotoComment.body.like(needle))
            .distinct()
        )
        q = q.where(Photo.id.in_(sub))
    if min_rating is not None and _has_table(db, "photo_ratings"):
        sub = (
            select(PhotoRating.photo_id)
            .where(PhotoRating.rating >= min_rating)
            .distinct()
        )
        q = q.where(Photo.id.in_(sub))
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
    if tag and _has_table(db, "tags"):
        sub = (
            select(PhotoTag.photo_id)
            .join(Tag, Tag.id == PhotoTag.tag_id)
            .where(func.lower(Tag.name) == tag.strip().lower())
        )
        q = q.where(Photo.id.in_(sub))
    if tag_q and _has_table(db, "tags"):
        needle = f"%{tag_q.strip().lower()}%"
        sub = (
            select(PhotoTag.photo_id)
            .join(Tag, Tag.id == PhotoTag.tag_id)
            .where(func.lower(Tag.name).like(needle))
            .distinct()
        )
        q = q.where(Photo.id.in_(sub))
    return q


def _apply_face_cluster_filter(q, db: Session, face_cluster_id: int | None):
    if face_cluster_id is None or not _has_table(db, "photo_faces"):
        return q
    from ..models import PhotoFace
    sub = (
        select(PhotoFace.photo_id)
        .where(PhotoFace.cluster_id == face_cluster_id)
        .distinct()
    )
    return q.where(Photo.id.in_(sub))


_KNOWN_TABLES: set[str] = set()


def _has_table(db: Session, table_name: str) -> bool:
    """True if `table_name` exists in the current DB.

    Guards search filters that reference 0004/0005 tables so the list
    endpoint keeps working on a DB that's been pulled forward in code
    but not yet `alembic upgrade head`-ed. Per-process cache once a
    table has been confirmed to exist.
    """
    if table_name in _KNOWN_TABLES:
        return True
    try:
        db.execute(text(f'SELECT 1 FROM "{table_name}" LIMIT 0'))
        _KNOWN_TABLES.add(table_name)
        return True
    except Exception:
        return False


@router.get("", response_model=PhotoPage)
def list_photos(
    root_id: int | None = None,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
    no_date_only: bool = Query(
        False,
        description=(
            "Return only photos whose taken_at is NULL. When true, "
            "date_from/date_to are ignored."
        ),
    ),
    status_filter: str = "active",
    text_q: str | None = Query(
        None,
        description=(
            "Unified search needle. Matches against filename, rel_path, "
            "description, comment body, and tag name (OR)."
        ),
    ),
    filename_q: str | None = Query(
        None,
        description="Match against filename + rel_path only (case-insensitive substring).",
    ),
    media_kind: str | None = Query(
        None,
        pattern="^(image|video)$",
        description="Restrict to one media kind. Omit for both.",
    ),
    comment_q: str | None = Query(None, description="comment substring (case-insensitive for ASCII)"),
    min_rating: int | None = Query(None, ge=1, le=5, description="any user's rating ≥ this"),
    near_lat: float | None = Query(None, ge=-90, le=90),
    near_lng: float | None = Query(None, ge=-180, le=180),
    near_radius_deg: float | None = Query(None, gt=0, le=10),
    tag: str | None = Query(None, description="filter to photos carrying this exact tag name"),
    tag_q: str | None = Query(None, description="substring match across all tag names"),
    face_cluster_id: int | None = Query(None, description="filter to photos containing a face from this cluster"),
    path_prefix: str | None = Query(None, description="rel_path prefix (folder browser)"),
    page: int = Query(1, ge=1),
    page_size: int = Query(60, ge=1, le=500),
    db: Session = Depends(get_db),
) -> PhotoPage:
    q = select(Photo)
    if status_filter:
        q = q.where(Photo.status == status_filter)
    if root_id is not None:
        q = q.where(Photo.root_id == root_id)
    if media_kind:
        q = q.where(Photo.media_kind == media_kind)
    if no_date_only:
        q = q.where(Photo.taken_at.is_(None))
    else:
        if date_from is not None:
            q = q.where(Photo.taken_at >= date_from)
        if date_to is not None:
            q = q.where(Photo.taken_at <= date_to)
    if path_prefix:
        if not path_prefix.endswith("/"):
            path_prefix = path_prefix + "/"
        q = q.where(Photo.rel_path.like(path_prefix + "%"))
    q = _apply_search_filters(
        q, db, comment_q, min_rating, near_lat, near_lng, near_radius_deg,
        tag=tag, tag_q=tag_q, text_q=text_q, filename_q=filename_q,
    )
    q = _apply_face_cluster_filter(q, db, face_cluster_id)

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


class ClusterOut(BaseModel):
    """One server-side aggregate cell for the map view.

    `count == 1` ⇒ singleton, render as a normal photo marker (use
    `sample_id` to open the lightbox). `count > 1` ⇒ cluster bubble,
    click to zoom in.
    """

    lat: float
    lng: float
    count: int
    sample_id: int


@router.get("/locations/clusters", response_model=list[ClusterOut])
def list_location_clusters(
    bbox: str = Query(..., description="'minLng,minLat,maxLng,maxLat'"),
    zoom: int = Query(..., ge=0, le=22),
    root_id: int | None = None,
    path_prefix: str | None = None,
    comment_q: str | None = None,
    tag_q: str | None = None,
    tag: str | None = None,
    min_rating: int | None = Query(None, ge=1, le=5),
    face_cluster_id: int | None = None,
    db: Session = Depends(get_db),
) -> list[ClusterOut]:
    """Group markers into grid cells sized to the requested zoom, so the
    map can render dense regions (home/work areas) with a handful of
    cluster bubbles instead of thousands of DOM markers.

    At zoom ≥ 16 the cell shrinks below typical GPS jitter, so each row
    effectively becomes its own marker — same result as the raw
    /locations endpoint but in this consolidated response shape.
    """
    try:
        min_lng, min_lat, max_lng, max_lat = (float(x) for x in bbox.split(","))
    except ValueError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"bad bbox: {e}")

    # cell ≈ 96 / 2**zoom degrees — picks a cell size that renders ~70 px
    # at the given zoom, so adjacent bubbles stay visually separated
    # instead of overlapping into the unreadable carpet they used to.
    cell = 96.0 / (2 ** max(zoom, 1))

    base = (
        select(
            (func.floor(PhotoLocation.latitude / cell) * cell).label("lat_bin"),
            (func.floor(PhotoLocation.longitude / cell) * cell).label("lng_bin"),
            func.count().label("cnt"),
            func.avg(PhotoLocation.latitude).label("avg_lat"),
            func.avg(PhotoLocation.longitude).label("avg_lng"),
            func.min(PhotoLocation.photo_id).label("sample_id"),
        )
        .join(Photo, Photo.id == PhotoLocation.photo_id)
        .where(
            Photo.status == "active",
            Photo.thumb_status.in_(("ok", "partial")),
            PhotoLocation.latitude.between(min_lat, max_lat),
            PhotoLocation.longitude.between(min_lng, max_lng),
        )
    )
    if root_id is not None:
        base = base.where(Photo.root_id == root_id)
    if path_prefix:
        if not path_prefix.endswith("/"):
            path_prefix = path_prefix + "/"
        base = base.where(Photo.rel_path.like(path_prefix + "%"))
    base = _apply_search_filters(
        base, db, comment_q, min_rating, None, None, None, tag=tag, tag_q=tag_q,
    )
    base = _apply_face_cluster_filter(base, db, face_cluster_id)

    base = base.group_by("lat_bin", "lng_bin")
    rows = db.execute(base).all()
    return [
        ClusterOut(
            lat=float(r.avg_lat),
            lng=float(r.avg_lng),
            count=int(r.cnt),
            sample_id=int(r.sample_id),
        )
        for r in rows
    ]


@router.get("/in-cell", response_model=list[PhotoOut])
def list_photos_in_cell(
    lat: float = Query(..., ge=-90, le=90),
    lng: float = Query(..., ge=-180, le=180),
    zoom: int = Query(..., ge=0, le=22),
    root_id: int | None = None,
    path_prefix: str | None = None,
    comment_q: str | None = None,
    tag_q: str | None = None,
    tag: str | None = None,
    min_rating: int | None = Query(None, ge=1, le=5),
    face_cluster_id: int | None = None,
    limit: int = Query(500, ge=1, le=2000),
    db: Session = Depends(get_db),
) -> list[PhotoOut]:
    """All photos in the grid cell that (lat,lng) falls into at the given
    zoom. Uses the same binning as /locations/clusters so the result is
    exactly the photos a cluster bubble represents — used by the map's
    max-zoom cluster click handler to seed the lightbox navigation list.
    """
    import math
    # MUST stay in sync with the cell formula in /locations/clusters above.
    cell = 96.0 / (2 ** max(zoom, 1))
    lat_bin = math.floor(lat / cell) * cell
    lng_bin = math.floor(lng / cell) * cell
    # Tiny epsilon so SQL float comparison doesn't drop boundary photos due
    # to floating-point noise (lat from DB vs lat_bin computed in Python).
    eps = cell * 1e-6

    q = (
        select(Photo)
        .join(PhotoLocation, Photo.id == PhotoLocation.photo_id)
        .where(
            Photo.status == "active",
            Photo.thumb_status.in_(("ok", "partial")),
            PhotoLocation.latitude >= lat_bin - eps,
            PhotoLocation.latitude < lat_bin + cell + eps,
            PhotoLocation.longitude >= lng_bin - eps,
            PhotoLocation.longitude < lng_bin + cell + eps,
        )
    )
    if root_id is not None:
        q = q.where(Photo.root_id == root_id)
    if path_prefix:
        if not path_prefix.endswith("/"):
            path_prefix = path_prefix + "/"
        q = q.where(Photo.rel_path.like(path_prefix + "%"))
    q = _apply_search_filters(
        q, db, comment_q, min_rating, None, None, None, tag=tag, tag_q=tag_q,
    )
    q = _apply_face_cluster_filter(q, db, face_cluster_id)
    q = q.order_by(Photo.taken_at.desc().nullslast(), Photo.id.desc()).limit(limit)

    rows = db.execute(q).scalars().all()
    return [PhotoOut.model_validate(r) for r in rows]


class InitialBboxOut(BaseModel):
    """Suggested initial view for the map — bbox around the densest cell.

    All four corners are None when no photo has GPS yet, so the frontend
    can fall back to a default world / country view.
    """

    min_lat: float | None = None
    max_lat: float | None = None
    min_lng: float | None = None
    max_lng: float | None = None
    photos_in_box: int = 0


@router.get("/locations/initial-bbox", response_model=InitialBboxOut)
def locations_initial_bbox(db: Session = Depends(get_db)) -> InitialBboxOut:
    """Pick the densest 0.1°×0.1° cell (~10 km) and return a ±0.5° box
    (~100 km on each side) around it.

    Used by the map view's initial render so we don't have to pull every
    marker upfront. The picker is unfiltered on purpose — the chosen
    starting region only seeds the viewport; the marker fetch that
    follows still honours the active search/folder filter.
    """
    row = db.execute(
        text(
            """
            SELECT
                ROUND(pl.latitude, 1)  AS lat_bin,
                ROUND(pl.longitude, 1) AS lng_bin,
                COUNT(*)               AS cnt
            FROM photo_locations pl
            JOIN photos p ON p.id = pl.photo_id
            WHERE p.status = 'active'
              AND p.thumb_status IN ('ok', 'partial')
            GROUP BY lat_bin, lng_bin
            ORDER BY cnt DESC, lat_bin, lng_bin
            LIMIT 1
            """
        )
    ).first()
    if row is None:
        return InitialBboxOut()
    center_lat = float(row.lat_bin)
    center_lng = float(row.lng_bin)
    half = 0.5  # ~55 km — diameter ~110 km
    return InitialBboxOut(
        min_lat=center_lat - half,
        max_lat=center_lat + half,
        min_lng=center_lng - half,
        max_lng=center_lng + half,
        photos_in_box=int(row.cnt or 0),
    )


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


class TagSummary(BaseModel):
    id: int
    name: str
    count: int
    source: str = "user"


@router.get("/tags", response_model=list[TagSummary])
def list_tags(
    source: str | None = Query(None, description="filter by tag origin (user / auto-yolo / auto-clip / face)"),
    db: Session = Depends(get_db),
) -> list[TagSummary]:
    """All tags with their photo counts — drives the autocomplete dropdown
    and the 주제 tab's category list. `source` filter lets the UI ask
    specifically for user-applied tags vs ML-generated ones.
    """
    q = (
        select(Tag.id, Tag.name, Tag.source, func.count(PhotoTag.photo_id).label("cnt"))
        .outerjoin(PhotoTag, PhotoTag.tag_id == Tag.id)
        .group_by(Tag.id)
        .order_by(func.count(PhotoTag.photo_id).desc(), Tag.name)
    )
    if source:
        q = q.where(Tag.source == source)
    rows = db.execute(q).all()
    return [
        TagSummary(id=r.id, name=r.name, count=int(r.cnt or 0), source=r.source or "user")
        for r in rows
    ]


class YearBucket(BaseModel):
    """One row in the timeline date histogram. `year=None` = photos without `taken_at`."""

    year: int | None
    count: int


@router.get("/date-histogram", response_model=list[YearBucket])
def date_histogram(
    root_id: int | None = None,
    path_prefix: str | None = None,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
    no_date_only: bool = Query(False),
    text_q: str | None = None,
    filename_q: str | None = None,
    media_kind: str | None = Query(None, pattern="^(image|video)$"),
    comment_q: str | None = None,
    min_rating: int | None = Query(None, ge=1, le=5),
    near_lat: float | None = Query(None, ge=-90, le=90),
    near_lng: float | None = Query(None, ge=-180, le=180),
    near_radius_deg: float | None = Query(None, gt=0, le=10),
    tag: str | None = None,
    tag_q: str | None = None,
    face_cluster_id: int | None = None,
    db: Session = Depends(get_db),
) -> list[YearBucket]:
    """Year buckets across the active timeline (newest first, no-date last).

    Accepts the same filters as list_photos so the right-side scrollbar
    represents the *filtered* range when a search or folder is active.
    """
    q = (
        select(
            func.strftime("%Y", Photo.taken_at).label("year"),
            func.count().label("count"),
        )
        .where(Photo.status == "active")
        .group_by("year")
    )
    if root_id is not None:
        q = q.where(Photo.root_id == root_id)
    if media_kind:
        q = q.where(Photo.media_kind == media_kind)
    if path_prefix:
        if not path_prefix.endswith("/"):
            path_prefix = path_prefix + "/"
        q = q.where(Photo.rel_path.like(path_prefix + "%"))
    if no_date_only:
        q = q.where(Photo.taken_at.is_(None))
    else:
        if date_from is not None:
            q = q.where(Photo.taken_at >= date_from)
        if date_to is not None:
            q = q.where(Photo.taken_at <= date_to)

    # Search filters (rating / comment / near / tag / text) go through the helper,
    # which uses `Photo.id.in_(subquery)` and needs a base selectable to attach to.
    if (
        text_q
        or filename_q
        or comment_q
        or min_rating is not None
        or (near_lat is not None and near_lng is not None)
        or tag
        or tag_q
        or face_cluster_id is not None
    ):
        base_filters = select(Photo.id).where(Photo.status == "active")
        base_filters = _apply_search_filters(
            base_filters, db, comment_q, min_rating, near_lat, near_lng, near_radius_deg,
            tag=tag, tag_q=tag_q, text_q=text_q, filename_q=filename_q,
        )
        base_filters = _apply_face_cluster_filter(base_filters, db, face_cluster_id)
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


class RootSummary(BaseModel):
    id: int
    label: str
    abs_path: str
    total_count: int


@router.get("/roots", response_model=list[RootSummary])
def list_visible_roots(db: Session = Depends(get_db)) -> list[RootSummary]:
    """Enabled roots with their active-photo counts (for the folder tab)."""
    rows = db.execute(
        select(Root, func.count(Photo.id))
        .outerjoin(
            Photo, (Photo.root_id == Root.id) & (Photo.status == "active")
        )
        .where(Root.enabled.is_(True))
        .group_by(Root.id)
        .order_by(Root.label)
    ).all()
    return [
        RootSummary(id=r.id, label=r.label, abs_path=r.abs_path, total_count=cnt or 0)
        for r, cnt in rows
    ]


class FolderChild(BaseModel):
    name: str
    count: int
    has_children: bool


class FolderListing(BaseModel):
    prefix: str
    direct_count: int   # photos directly at this prefix (not in any subfolder)
    children: list[FolderChild]


@router.get("/folders", response_model=FolderListing)
def list_folders(
    root_id: int = Query(..., description="which root to walk"),
    prefix: str = Query("", description="rel_path prefix; empty = root of the tree"),
    db: Session = Depends(get_db),
) -> FolderListing:
    """Return the immediate subfolders under (root_id, prefix) with photo counts.

    The folder hierarchy isn't stored as such — we derive it from the
    distinct prefixes of `photos.rel_path`. SQLite uses the
    (root_id, rel_path) unique-index for the LIKE 'prefix%' lookup, so
    walking deep folders is cheap; the top-level call still scans the
    whole root but only returns the first segments.
    """
    if prefix and not prefix.endswith("/"):
        prefix = prefix + "/"

    q = select(Photo.rel_path).where(
        Photo.root_id == root_id,
        Photo.status == "active",
    )
    if prefix:
        q = q.where(Photo.rel_path.like(prefix + "%"))

    plen = len(prefix)
    children: dict[str, dict] = {}
    direct_count = 0
    for (rp,) in db.execute(q):
        after = rp[plen:]
        slash = after.find("/")
        if slash < 0:
            direct_count += 1
            continue
        child = after[:slash]
        entry = children.setdefault(child, {"count": 0, "has_children": False})
        entry["count"] += 1
        # Cheap "does this subfolder itself contain subfolders?" — used by the
        # UI to decide whether to show an expand caret.
        if after.find("/", slash + 1) >= 0:
            entry["has_children"] = True

    return FolderListing(
        prefix=prefix,
        direct_count=direct_count,
        children=sorted(
            [
                FolderChild(name=name, count=v["count"], has_children=v["has_children"])
                for name, v in children.items()
            ],
            key=lambda c: c.name,
        ),
    )


@router.get("/locations", response_model=list[MarkerOut])
def list_locations(
    bbox: str | None = Query(
        None,
        description="Filter: 'minLng,minLat,maxLng,maxLat'. Omit to return everything.",
    ),
    root_id: int | None = None,
    path_prefix: str | None = None,
    comment_q: str | None = None,
    tag_q: str | None = None,
    tag: str | None = None,
    min_rating: int | None = Query(None, ge=1, le=5),
    face_cluster_id: int | None = None,
    # Cap bumped to 250k so libraries that have grown past the old 50k cap
    # still see every marker. The payload is lat/lng/id only (~30 bytes per
    # row), so 100k ≈ 3 MB — Leaflet.markercluster handles that fine via
    # chunkedLoading on the frontend.
    limit: int = Query(5000, ge=1, le=250_000),
    db: Session = Depends(get_db),
) -> list[MarkerOut]:
    """Lightweight marker list for the map view. Lat/Lng only.

    Accepts the same filters as list_photos so the header search and the
    folder/topic sidebar selections also constrain map markers.

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
    if root_id is not None:
        q = q.where(Photo.root_id == root_id)
    if path_prefix:
        if not path_prefix.endswith("/"):
            path_prefix = path_prefix + "/"
        q = q.where(Photo.rel_path.like(path_prefix + "%"))
    # Search filters (comment / tag / rating / face cluster) — `near_*` is
    # excluded on purpose: clamping the map to a small radius around its own
    # markers would be tautological.
    q = _apply_search_filters(
        q, db, comment_q, min_rating, None, None, None, tag=tag, tag_q=tag_q,
    )
    q = _apply_face_cluster_filter(q, db, face_cluster_id)

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
    # Editorial (added in 0005 migration).
    taken_at_original: datetime | None = None  # EXIF original, only set after taken_at was edited
    description: str | None = None
    tags: list[str] = []


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

    # Editorial fields (0005 migration). Tags are joined in alphabetically.
    try:
        out.taken_at_original = p.taken_at_original
        out.description = p.description
        tag_rows = db.execute(
            select(Tag.name)
            .join(PhotoTag, PhotoTag.tag_id == Tag.id)
            .where(PhotoTag.photo_id == photo_id)
            .order_by(Tag.name)
        ).all()
        out.tags = [r[0] for r in tag_rows]
    except Exception:
        pass

    return out


# ---- bulk operations (multi-select grid → delete / zip download) ----

class BulkActionIn(BaseModel):
    photo_ids: list[int]


_BULK_LIMIT = 1000
_DOWNLOAD_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{16,48}$")


def _sweep_old_downloads(max_age_seconds: int = 3600) -> None:
    """Best-effort cleanup of stale bulk-download zips left over after
    failed/cancelled downloads. Called from prepare; cheap on small sets."""
    now = time.time()
    try:
        for f in TMP_DIR.glob("download_*.zip"):
            try:
                if now - f.stat().st_mtime > max_age_seconds:
                    f.unlink()
            except OSError:
                pass
    except OSError:
        pass


def _unique_arc_name(taken: set, name: str) -> str:
    """Pick a name not already in `taken`, appending _2 / _3 if needed."""
    if name not in taken:
        taken.add(name)
        return name
    if "." in name:
        base, _, ext = name.rpartition(".")
        ext = "." + ext
    else:
        base, ext = name, ""
    i = 2
    while True:
        candidate = f"{base}_{i}{ext}"
        if candidate not in taken:
            taken.add(candidate)
            return candidate
        i += 1


@router.post("/bulk-delete")
def bulk_delete(
    payload: BulkActionIn,
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
) -> dict:
    """Move every selected photo to data/trash/ in one shot. Admin-only,
    same policy as the single DELETE."""
    if not payload.photo_ids:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "사진을 선택하세요")
    if len(payload.photo_ids) > _BULK_LIMIT:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"한 번에 {_BULK_LIMIT}장까지 가능합니다",
        )

    rows = db.execute(
        select(Photo).where(Photo.id.in_(payload.photo_ids))
    ).scalars().all()
    roots_map = {r.id: r for r in db.execute(select(Root)).scalars().all()}

    deleted: list[int] = []
    failed: list[int] = []
    for p in rows:
        root = roots_map.get(p.root_id)
        if root is None:
            failed.append(p.id)
            continue
        try:
            _move_to_trash(p, root, user)
        except Exception as e:
            log.warning("bulk_delete move failed for photo %s: %s", p.id, e)
        if p.status != "trashed":
            p.status = "trashed"
        deleted.append(p.id)
    db.commit()
    return {"deleted": len(deleted), "failed": failed, "ids": deleted}


@router.post("/bulk-download")
def bulk_download_prepare(
    payload: BulkActionIn,
    user: User = Depends(require_auth),
    db: Session = Depends(get_db),
) -> dict:
    """Build a one-shot ZIP of the requested photos in data/tmp/ and return
    a token URL to stream it via /bulk-download/{token}. The zip is deleted
    after the first successful GET (or swept after an hour)."""
    if not payload.photo_ids:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "사진을 선택하세요")
    if len(payload.photo_ids) > _BULK_LIMIT:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"한 번에 {_BULK_LIMIT}장까지 가능합니다",
        )

    _sweep_old_downloads()
    TMP_DIR.mkdir(parents=True, exist_ok=True)

    rows = db.execute(
        select(Photo)
        .where(Photo.id.in_(payload.photo_ids), Photo.status == "active")
    ).scalars().all()
    if not rows:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "유효한 사진이 없습니다")
    roots_map = {r.id: r for r in db.execute(select(Root)).scalars().all()}

    token = secrets.token_urlsafe(18)
    tmp_path = TMP_DIR / f"download_{token}.zip"

    arc_names: set[str] = set()
    added: int = 0
    skipped: list[int] = []
    try:
        # ZIP_STORED — JPEGs/PNGs/HEICs are already compressed; CPU spent on
        # DEFLATE would be wasted. Just bundle.
        with zipfile.ZipFile(tmp_path, "w", zipfile.ZIP_STORED, allowZip64=True) as zf:
            for p in rows:
                root = roots_map.get(p.root_id)
                if root is None:
                    skipped.append(p.id)
                    continue
                src = Path(join_root(root.abs_path, p.rel_path))
                if not src.exists():
                    skipped.append(p.id)
                    continue
                arcname = _unique_arc_name(arc_names, p.filename or f"photo_{p.id}")
                try:
                    zf.write(src, arcname=arcname)
                except OSError as e:
                    log.warning("zip add failed for photo %s: %s", p.id, e)
                    skipped.append(p.id)
                    continue
                added += 1
    except Exception as e:
        try:
            tmp_path.unlink()
        except OSError:
            pass
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR, f"zip 생성 실패: {e}"
        )

    if added == 0:
        try:
            tmp_path.unlink()
        except OSError:
            pass
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, "압축할 수 있는 사진이 없습니다"
        )

    fname = f"myphotos-{added}.zip"
    return {
        "url": f"/api/photos/bulk-download/{token}",
        "filename": fname,
        "added": added,
        "skipped": skipped,
    }


@router.get("/bulk-download/{token}")
def bulk_download_fetch(
    token: str,
    background_tasks: BackgroundTasks,
    user: User = Depends(require_auth),
) -> FileResponse:
    if not _DOWNLOAD_TOKEN_RE.match(token):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "잘못된 토큰")
    path = TMP_DIR / f"download_{token}.zip"
    if not path.exists():
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, "다운로드 만료 또는 없음 (다시 시도)"
        )
    # Delete after the response finishes streaming.
    background_tasks.add_task(_safe_unlink, path)
    return FileResponse(
        path, filename="myphotos.zip", media_type="application/zip"
    )


def _safe_unlink(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        pass


# ---- editorial fields (taken_at, description, tags) ----

class TakenAtIn(BaseModel):
    # null → revert to the EXIF original (if we have one snapshotted)
    taken_at: datetime | None = None


@router.put("/{photo_id}/taken-at")
def set_taken_at(
    photo_id: int,
    payload: TakenAtIn,
    user: User = Depends(require_auth),
    db: Session = Depends(get_db),
) -> dict:
    p = db.get(Photo, photo_id)
    if p is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    if payload.taken_at is None:
        # Revert: only meaningful if we snapshotted an original.
        if p.taken_at_original is not None:
            p.taken_at = p.taken_at_original
            p.taken_at_original = None
    else:
        # First edit snapshots the EXIF value so we can revert later.
        if p.taken_at_original is None and p.taken_at is not None:
            p.taken_at_original = p.taken_at
        p.taken_at = payload.taken_at
    db.commit()
    return {
        "ok": True,
        "taken_at": p.taken_at.isoformat() if p.taken_at else None,
        "taken_at_original": p.taken_at_original.isoformat() if p.taken_at_original else None,
    }


class DescriptionIn(BaseModel):
    description: str | None = None


@router.put("/{photo_id}/description")
def set_description(
    photo_id: int,
    payload: DescriptionIn,
    user: User = Depends(require_auth),
    db: Session = Depends(get_db),
) -> dict:
    p = db.get(Photo, photo_id)
    if p is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    body = (payload.description or "").strip()
    p.description = body or None
    db.commit()
    return {"ok": True, "description": p.description}


class TagsIn(BaseModel):
    tags: list[str]


@router.put("/{photo_id}/tags", response_model=list[str])
def set_photo_tags(
    photo_id: int,
    payload: TagsIn,
    user: User = Depends(require_auth),
    db: Session = Depends(get_db),
) -> list[str]:
    p = db.get(Photo, photo_id)
    if p is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND)

    # Dedupe case-insensitively but preserve the user's typed casing for
    # whichever variant came in first.
    seen: dict[str, str] = {}
    for raw in payload.tags or []:
        name = (raw or "").strip()
        if not name:
            continue
        if len(name) > 64:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST, f"태그 '{name[:20]}…'이 너무 깁니다 (최대 64자)"
            )
        key = name.lower()
        seen.setdefault(key, name)

    # Resolve / create each tag row.
    tag_ids: list[int] = []
    for name in seen.values():
        existing = db.execute(
            select(Tag).where(func.lower(Tag.name) == name.lower())
        ).scalar_one_or_none()
        if existing is None:
            t = Tag(name=name)
            db.add(t)
            db.flush()
            tag_ids.append(t.id)
        else:
            tag_ids.append(existing.id)

    # Replace the photo's tag set atomically.
    from sqlalchemy import delete as _delete
    db.execute(_delete(PhotoTag).where(PhotoTag.photo_id == photo_id))
    for tid in tag_ids:
        db.add(PhotoTag(photo_id=photo_id, tag_id=tid))
    db.commit()

    # Return the resolved names ordered alphabetically for stable UI.
    final = db.execute(
        select(Tag.name)
        .join(PhotoTag, PhotoTag.tag_id == Tag.id)
        .where(PhotoTag.photo_id == photo_id)
        .order_by(Tag.name)
    ).all()
    return [r[0] for r in final]


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

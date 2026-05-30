"""Photo browsing API — list + thumbnail/original serving.

MVP 2 supports basic filtering (root, date range, status) and offset
pagination. Map/cluster endpoints land in a later MVP.
"""

from __future__ import annotations

import functools
import json
import logging
import mimetypes
import os
import re
import secrets
import shutil
import subprocess
import time
from datetime import datetime
from io import BytesIO
from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, status
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, Field
from sqlalchemy import func, or_, select, text
from sqlalchemy.orm import Session

from ..auth import (
    require_admin,
    require_auth,
    require_can_delete,
    require_can_edit_meta_others,
)
from .. import audit
from ..auth_acl import (
    apply_visible_photo_filter,
    hidden_root_ids,
    require_folder_level,
    require_photo_ids_level,
    require_photo_level,
    require_root_level,
)
from .. import fts as _fts
from ..config import get_settings
from ..external import exiftool_path
from ..models import (
    Photo, PhotoAutoTag, PhotoComment, PhotoLocation, PhotoRating,
    PhotoTag, Root, Tag, User,
)
from ..paths import TMP_DIR, TRASH_DIR
from ..scanner.utils import escape_like, join_root
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
    # When set, this photo is one half of a Live Photo pair (iPhone
    # HEIC↔MOV) — the lightbox uses it to surface the "▶ Live" toggle.
    companion_id: int | None = None
    # Surface root.readonly so the UI can disable delete buttons up-front
    # instead of round-tripping a 409. Populated lazily where it matters
    # (single GET, list, in-cell, duplicates) — None means "didn't check".
    root_readonly: bool | None = None
    # Per-photo visibility (P4): 'inherit' | 'private' | 'public'
    visibility: str = "inherit"
    owner_user_id: int | None = None
    # Filesystem-side write check populated by get_photo / get_details:
    # `writable` is False when rotate/delete would fail (root readonly,
    # parent dir not writable to the API process, file missing, etc.);
    # `write_block_reason` carries the user-facing explanation. None for
    # both = "we didn't probe", e.g. on list payloads where checking
    # per-photo would add filesystem syscalls to every page load.
    writable: bool | None = None
    write_block_reason: str | None = None

    class Config:
        from_attributes = True


class PhotoCard(BaseModel):
    """Lightweight model for grid/map list responses.

    Strips fields the grid never reads (exif/thumb/lifecycle statuses)
    so payload size stays small — meaningful when the timeline pages
    60 photos at a time and the map can return up to 500 per cell.
    Endpoints that need full data (single GET, /details) keep using
    PhotoOut.

    sha256 IS exposed (despite size) because the gallery appends its
    first 8 chars as a `?v=` cache-buster on every thumbnail URL —
    when the file's contents change (e.g. a rotation rewrote the EXIF
    block) the new sha256 lands here and the browser fetches fresh
    instead of serving the immutable-cached old thumb.
    """

    id: int
    root_id: int
    rel_path: str
    filename: str
    media_kind: str
    ext: str
    sha256: str | None = None
    taken_at: datetime | None = None
    width: int | None = None
    height: int | None = None
    camera_model: str | None = None
    # Same Live Photo signal as PhotoOut — the grid needs it for the
    # tile overlay; lightbox reads it from /details on click.
    companion_id: int | None = None

    class Config:
        from_attributes = True


class PhotoPage(BaseModel):
    total: int
    page: int
    page_size: int
    items: list[PhotoCard]


def _apply_scalar_filters(
    q,
    *,
    root_id: int | None = None,
    path_prefix: str | None = None,
    media_kind: str | None = None,
    include_companion_videos: bool = True,
    min_size_kb: int | None = None,
    max_size_kb: int | None = None,
    no_date_only: bool = False,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
    owner_user_id: int | None = None,
):
    """Apply the scalar (non-search, non-ACL) filters every photo
    listing endpoint shares.

    Extracted from list_photos, list_location_clusters,
    list_photos_in_cell, date_histogram — those four had the same
    block copy-pasted with subtle drift. One source of truth here
    means changing the Live-Photo MOV exclusion (or adding a new
    filter) only edits one place.

    Callers pass:
        - the *visibility-filtered* selectable (apply_visible_photo_filter first)
        - the search filters via _apply_search_filters AFTER this call

    Kwargs-only so callers can be explicit; None / falsy means "no
    filter on that axis." include_companion_videos defaults to True
    here because the caller-side default varies (the listing wants
    False, the cluster query inherits the default). Each endpoint
    sets it explicitly to be safe.
    """
    if root_id is not None:
        q = q.where(Photo.root_id == root_id)
    if path_prefix:
        if not path_prefix.endswith("/"):
            path_prefix = path_prefix + "/"
        q = q.where(Photo.rel_path.like(escape_like(path_prefix) + "%", escape="\\"))
    if media_kind:
        q = q.where(Photo.media_kind == media_kind)
    if not include_companion_videos:
        # Hide the MOV side of Live Photo pairs.
        q = q.where(
            ~((Photo.media_kind == "video") & (Photo.companion_id.is_not(None)))
        )
    if min_size_kb is not None:
        q = q.where(Photo.file_size >= min_size_kb * 1024)
    if max_size_kb is not None:
        q = q.where(Photo.file_size <= max_size_kb * 1024)
    if no_date_only:
        q = q.where(Photo.taken_at.is_(None))
    else:
        if date_from is not None:
            q = q.where(Photo.taken_at >= date_from)
        if date_to is not None:
            q = q.where(Photo.taken_at <= date_to)
    if owner_user_id is not None:
        # 0 = "no uploader recorded" (legacy / scanner imports).
        if owner_user_id == 0:
            q = q.where(Photo.owner_user_id.is_(None))
        else:
            q = q.where(Photo.owner_user_id == owner_user_id)
    return q


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
        # Unified search across filename + rel_path + description +
        # comment body + tag name + uploader. Prefer the FTS5 trigram
        # index (sub-100ms at 100k rows) and fall back to the OR-of-
        # LIKE plan when (a) the migration hasn't run yet or (b) the
        # query is shorter than the trigram minimum of 3 chars.
        needle_str = text_q.strip()
        match_expr = _fts.build_match_query(needle_str)
        if match_expr is not None and _fts.is_available(db):
            q = q.where(
                Photo.id.in_(
                    text(_fts.matching_photo_ids_sql()).bindparams(match=match_expr)
                )
            )
        else:
            needle = f"%{needle_str}%"
            n_lower = f"%{needle_str.lower()}%"
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
            # ML-generated auto tags share the same Tag dictionary but live in
            # photo_auto_tags. Include them in the same OR so a search for
            # "고양이" returns photos whether the label came from the user or
            # from YOLO/CLIP/face.
            if _has_table(db, "photo_auto_tags"):
                conds.append(
                    Photo.id.in_(
                        select(PhotoAutoTag.photo_id)
                        .join(Tag, Tag.id == PhotoAutoTag.tag_id)
                        .where(func.lower(Tag.name).like(n_lower))
                    )
                )
            # Uploader name match — "scsung" should also bring back photos
            # this user uploaded, so the unified search subsumes what the
            # separate uploader dropdown used to do. Photos with
            # owner_user_id IS NULL (legacy / scanner-imported) never match
            # since there's no uploader to compare against.
            conds.append(
                Photo.owner_user_id.in_(
                    select(User.id).where(func.lower(User.username).like(n_lower))
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
        needle = tag.strip().lower()
        # Match either user-applied tags or ML auto labels with this
        # exact name. Chip clicks shouldn't care which side put it on.
        user_sub = (
            select(PhotoTag.photo_id)
            .join(Tag, Tag.id == PhotoTag.tag_id)
            .where(func.lower(Tag.name) == needle)
        )
        if _has_table(db, "photo_auto_tags"):
            auto_sub = (
                select(PhotoAutoTag.photo_id)
                .join(Tag, Tag.id == PhotoAutoTag.tag_id)
                .where(func.lower(Tag.name) == needle)
            )
            q = q.where(or_(Photo.id.in_(user_sub), Photo.id.in_(auto_sub)))
        else:
            q = q.where(Photo.id.in_(user_sub))
    if tag_q and _has_table(db, "tags"):
        needle = f"%{tag_q.strip().lower()}%"
        user_sub = (
            select(PhotoTag.photo_id)
            .join(Tag, Tag.id == PhotoTag.tag_id)
            .where(func.lower(Tag.name).like(needle))
            .distinct()
        )
        if _has_table(db, "photo_auto_tags"):
            auto_sub = (
                select(PhotoAutoTag.photo_id)
                .join(Tag, Tag.id == PhotoAutoTag.tag_id)
                .where(func.lower(Tag.name).like(needle))
                .distinct()
            )
            q = q.where(or_(Photo.id.in_(user_sub), Photo.id.in_(auto_sub)))
        else:
            q = q.where(Photo.id.in_(user_sub))
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
    include_companion_videos: bool = Query(
        False,
        description=(
            "By default the MOV half of a Live Photo pair is hidden from "
            "listings — the still (HEIC) is the primary view and the "
            "lightbox plays the MOV on demand. Set true to include both."
        ),
    ),
    min_size_kb: int | None = Query(
        None,
        ge=0,
        description="Minimum file size in KiB (inclusive). Useful for excluding tiny screenshots/memes.",
    ),
    max_size_kb: int | None = Query(
        None,
        ge=0,
        description="Maximum file size in KiB (inclusive).",
    ),
    comment_q: str | None = Query(None, description="comment substring (case-insensitive for ASCII)"),
    min_rating: int | None = Query(None, ge=1, le=5, description="any user's rating ≥ this"),
    near_lat: float | None = Query(None, ge=-90, le=90),
    near_lng: float | None = Query(None, ge=-180, le=180),
    near_radius_deg: float | None = Query(None, gt=0, le=10),
    tag: str | None = Query(None, description="filter to photos carrying this exact tag name"),
    tag_q: str | None = Query(None, description="substring match across all tag names"),
    face_cluster_id: int | None = Query(None, description="filter to photos containing a face from this cluster"),
    owner_user_id: int | None = Query(
        None,
        description=(
            "Filter to photos uploaded by this user. Use 0 to filter "
            "to photos with no recorded uploader (legacy / scanner-imported)."
        ),
    ),
    path_prefix: str | None = Query(None, description="rel_path prefix (folder browser)"),
    page: int = Query(1, ge=1),
    page_size: int = Query(60, ge=1, le=500),
    with_total: bool = Query(
        True,
        description="Compute total row count. Infinite-scroll clients should "
        "send true on page 1 and false thereafter — the count is the same "
        "and re-running COUNT(*) over the filtered set each page is expensive.",
    ),
    user: User = Depends(require_auth),
    db: Session = Depends(get_db),
) -> PhotoPage:
    q = select(Photo)
    q = apply_visible_photo_filter(q, db, user)
    if status_filter:
        q = q.where(Photo.status == status_filter)
    q = _apply_scalar_filters(
        q,
        root_id=root_id, path_prefix=path_prefix,
        media_kind=media_kind, include_companion_videos=include_companion_videos,
        min_size_kb=min_size_kb, max_size_kb=max_size_kb,
        no_date_only=no_date_only, date_from=date_from, date_to=date_to,
        owner_user_id=owner_user_id,
    )
    q = _apply_search_filters(
        q, db, comment_q, min_rating, near_lat, near_lng, near_radius_deg,
        tag=tag, tag_q=tag_q, text_q=text_q, filename_q=filename_q,
    )
    q = _apply_face_cluster_filter(q, db, face_cluster_id)

    total = (
        db.execute(select(func.count()).select_from(q.subquery())).scalar_one()
        if with_total else -1
    )
    rows = db.execute(
        q.order_by(Photo.taken_at.desc().nullslast(), Photo.mtime.desc().nullslast(), Photo.id.desc())
         .offset((page - 1) * page_size)
         .limit(page_size)
    ).scalars().all()
    return PhotoPage(
        total=total,
        page=page,
        page_size=page_size,
        items=[PhotoCard.model_validate(r) for r in rows],
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
    date_from: datetime | None = None,
    date_to: datetime | None = None,
    no_date_only: bool = Query(False),
    text_q: str | None = None,
    filename_q: str | None = None,
    media_kind: str | None = Query(None, pattern="^(image|video)$"),
    min_size_kb: int | None = Query(None, ge=0),
    max_size_kb: int | None = Query(None, ge=0),
    comment_q: str | None = None,
    tag_q: str | None = None,
    tag: str | None = None,
    min_rating: int | None = Query(None, ge=1, le=5),
    face_cluster_id: int | None = None,
    owner_user_id: int | None = Query(
        None, description="Filter to photos uploaded by this user (0 = no recorded uploader)."
    ),
    user: User = Depends(require_auth),
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
    base = apply_visible_photo_filter(base, db, user)
    base = _apply_scalar_filters(
        base,
        root_id=root_id, path_prefix=path_prefix,
        media_kind=media_kind,
        min_size_kb=min_size_kb, max_size_kb=max_size_kb,
        no_date_only=no_date_only, date_from=date_from, date_to=date_to,
        owner_user_id=owner_user_id,
    )
    base = _apply_search_filters(
        base, db, comment_q, min_rating, None, None, None,
        tag=tag, tag_q=tag_q, text_q=text_q, filename_q=filename_q,
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


@router.get("/in-cell", response_model=list[PhotoCard])
def list_photos_in_cell(
    lat: float = Query(..., ge=-90, le=90),
    lng: float = Query(..., ge=-180, le=180),
    zoom: int = Query(..., ge=0, le=22),
    root_id: int | None = None,
    path_prefix: str | None = None,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
    no_date_only: bool = Query(False),
    text_q: str | None = None,
    filename_q: str | None = None,
    media_kind: str | None = Query(None, pattern="^(image|video)$"),
    min_size_kb: int | None = Query(None, ge=0),
    max_size_kb: int | None = Query(None, ge=0),
    comment_q: str | None = None,
    tag_q: str | None = None,
    tag: str | None = None,
    min_rating: int | None = Query(None, ge=1, le=5),
    face_cluster_id: int | None = None,
    owner_user_id: int | None = Query(
        None, description="Filter to photos uploaded by this user (0 = no recorded uploader)."
    ),
    limit: int = Query(500, ge=1, le=2000),
    user: User = Depends(require_auth),
    db: Session = Depends(get_db),
) -> list[PhotoCard]:
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
    q = apply_visible_photo_filter(q, db, user)
    q = _apply_scalar_filters(
        q,
        root_id=root_id, path_prefix=path_prefix,
        media_kind=media_kind,
        min_size_kb=min_size_kb, max_size_kb=max_size_kb,
        no_date_only=no_date_only, date_from=date_from, date_to=date_to,
        owner_user_id=owner_user_id,
    )
    q = _apply_search_filters(
        q, db, comment_q, min_rating, None, None, None,
        tag=tag, tag_q=tag_q, text_q=text_q, filename_q=filename_q,
    )
    q = _apply_face_cluster_filter(q, db, face_cluster_id)
    q = q.order_by(Photo.taken_at.desc().nullslast(), Photo.mtime.desc().nullslast(), Photo.id.desc()).limit(limit)

    rows = db.execute(q).scalars().all()
    return [PhotoCard.model_validate(r) for r in rows]


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
def locations_initial_bbox(
    user: User = Depends(require_auth),
    db: Session = Depends(get_db),
) -> InitialBboxOut:
    """Pick the densest 0.1°×0.1° cell (~10 km) and return a ±0.5° box
    (~100 km on each side) around it.

    Used by the map view's initial render so we don't have to pull every
    marker upfront. The picker is unfiltered on purpose — the chosen
    starting region only seeds the viewport; the marker fetch that
    follows still honours the active search/folder filter. ACL: photos
    in hidden roots are excluded so the initial viewport doesn't drift
    to a region the user can't actually see.
    """
    hidden = hidden_root_ids(db, user)
    if hidden:
        # Build the parameter list dynamically for the IN clause.
        placeholders = ",".join(f":h{i}" for i in range(len(hidden)))
        params = {f"h{i}": rid for i, rid in enumerate(hidden)}
        sql = (
            f"SELECT ROUND(pl.latitude, 1) AS lat_bin, "
            f"       ROUND(pl.longitude, 1) AS lng_bin, "
            f"       COUNT(*) AS cnt "
            f"FROM photo_locations pl "
            f"JOIN photos p ON p.id = pl.photo_id "
            f"WHERE p.status = 'active' "
            f"  AND p.thumb_status IN ('ok','partial') "
            f"  AND p.root_id NOT IN ({placeholders}) "
            f"GROUP BY lat_bin, lng_bin "
            f"ORDER BY cnt DESC, lat_bin, lng_bin "
            f"LIMIT 1"
        )
        row = db.execute(text(sql), params).first()
    else:
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


@router.get("/nearby", response_model=list[PhotoCard])
def list_nearby(
    photo_id: int = Query(..., description="anchor photo id"),
    radius_deg: float = Query(0.005, gt=0, le=1.0,
                              description="lat/lng degrees (0.005 ≈ ~500m)"),
    limit: int = Query(100, ge=1, le=500),
    user: User = Depends(require_auth),
    db: Session = Depends(get_db),
) -> list[PhotoCard]:
    """Photos taken within `radius_deg` of the anchor photo's GPS coordinates.

    Powers the map → lightbox flow: clicking a marker opens the lightbox
    over this set, so prev/next and the filmstrip surface neighboring
    photos taken at the same location. Result is ordered by taken_at desc
    (matches the timeline order), the anchor is included.
    """
    anchor = db.get(Photo, photo_id)
    if anchor is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "anchor photo not found")
    # If the user can't see the anchor (root hidden for them), pretend
    # it doesn't exist — same response shape as a missing photo so we
    # don't reveal its existence.
    require_photo_level(db, user, anchor, "read")
    loc = db.get(PhotoLocation, photo_id)
    if loc is None:
        # No GPS on the anchor — just hand back the photo by itself.
        return [PhotoCard.model_validate(anchor)]

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
        .order_by(Photo.taken_at.desc().nullslast(), Photo.mtime.desc().nullslast(), Photo.id.desc())
        .limit(limit)
    )
    q = apply_visible_photo_filter(q, db, user)
    rows = db.execute(q).scalars().all()
    return [PhotoCard.model_validate(r) for r in rows]


class TagSummary(BaseModel):
    id: int
    name: str
    count: int
    source: str = "user"


# Small in-memory cache for the tag summary. The query joins photos
# against (photo_tags ∪ photo_auto_tags), groups, and counts — fine for
# small libraries, but the autocomplete dropdown hits it on every input
# focus + key stroke, so 10–60s of staleness is a fair price for the
# instant response. Keyed on the `source` filter; mutates invalidate
# nothing — TTL is the only correctness mechanism.
import time as _time
# Key is (source, hidden_root_ids_tuple) so admins / restricted users
# don't share cached entries — see list_tags below.
_TAGS_CACHE: dict[tuple, tuple[float, list]] = {}
_TAGS_CACHE_TTL_S = 30.0


@router.get("/tags", response_model=list[TagSummary])
def list_tags(
    source: str | None = Query(
        None,
        description="user | auto-yolo | auto-clip | face. Omit for everything.",
    ),
    # Gallery filter bar — same set as list_photos so the 주제 tab's
    # counts shrink to match a filtered timeline (e.g. 2023년만 / 동영상만).
    # All filters chain as AND.
    root_id: int | None = None,
    path_prefix: str | None = None,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
    no_date_only: bool = Query(False),
    text_q: str | None = None,
    filename_q: str | None = None,
    media_kind: str | None = Query(None, pattern="^(image|video)$"),
    min_size_kb: int | None = Query(None, ge=0),
    max_size_kb: int | None = Query(None, ge=0),
    comment_q: str | None = None,
    min_rating: int | None = Query(None, ge=1, le=5),
    near_lat: float | None = Query(None, ge=-90, le=90),
    near_lng: float | None = Query(None, ge=-180, le=180),
    near_radius_deg: float | None = Query(None, gt=0, le=10),
    tag: str | None = None,
    tag_q: str | None = None,
    face_cluster_id: int | None = None,
    owner_user_id: int | None = Query(
        None, description="Filter to photos uploaded by this user (0 = no recorded uploader)."
    ),
    user: User = Depends(require_auth),
    db: Session = Depends(get_db),
) -> list[TagSummary]:
    """All tag-by-source pairings with photo counts.

    Same tag name can show up multiple times with different sources
    (e.g. "person" counted once from photo_tags as user='user' and
    once from photo_auto_tags grouped by source). That's intentional
    — the 주제 tab wants to distinguish "I tagged this" from "YOLO
    saw this" even when the words match.

    When any filter is set, counts are restricted to photos that match
    every filter (AND). The unfiltered case (no params other than
    `source`) is cached because it's the heaviest query and the most
    common call from the 주제 tab.

    ACL (P2+): photos in roots the caller can't see are dropped from
    the counts. Cache key folds in the caller's hidden-root fingerprint
    so an admin's cached result is never served to a viewer with
    restricted roots (and vice versa).
    """
    # Detect "any filter set" — if so we bypass the cache because the
    # cache key would explode across every possible combination.
    _filtered = any((
        root_id is not None, path_prefix,
        date_from is not None, date_to is not None, no_date_only,
        text_q, filename_q,
        media_kind,
        min_size_kb is not None, max_size_kb is not None,
        comment_q, min_rating is not None,
        near_lat is not None, near_lng is not None,
        tag, tag_q, face_cluster_id is not None,
        owner_user_id is not None,
    ))

    # ACL filter — drop photos in hidden roots from every count.
    hidden = hidden_root_ids(db, user)
    cache_key = (source, tuple(sorted(hidden)))
    if not _filtered:
        cached = _TAGS_CACHE.get(cache_key)
        if cached is not None:
            ts, payload = cached
            if _time.monotonic() - ts < _TAGS_CACHE_TTL_S:
                return payload

    from ..models import PhotoAutoTag

    # Build the filtered photo-id selectable once and reuse for both
    # user-tag and auto-tag aggregates so the AND set is identical.
    photo_ids = select(Photo.id).where(Photo.status == "active")
    if hidden:
        photo_ids = photo_ids.where(~Photo.root_id.in_(hidden))
    if root_id is not None:
        photo_ids = photo_ids.where(Photo.root_id == root_id)
    if path_prefix:
        if not path_prefix.endswith("/"):
            path_prefix = path_prefix + "/"
        photo_ids = photo_ids.where(Photo.rel_path.like(escape_like(path_prefix) + "%", escape="\\"))
    if owner_user_id is not None:
        if owner_user_id == 0:
            photo_ids = photo_ids.where(Photo.owner_user_id.is_(None))
        else:
            photo_ids = photo_ids.where(Photo.owner_user_id == owner_user_id)
    if media_kind:
        photo_ids = photo_ids.where(Photo.media_kind == media_kind)
    if min_size_kb is not None:
        photo_ids = photo_ids.where(Photo.file_size >= min_size_kb * 1024)
    if max_size_kb is not None:
        photo_ids = photo_ids.where(Photo.file_size <= max_size_kb * 1024)
    if no_date_only:
        photo_ids = photo_ids.where(Photo.taken_at.is_(None))
    else:
        if date_from is not None:
            photo_ids = photo_ids.where(Photo.taken_at >= date_from)
        if date_to is not None:
            photo_ids = photo_ids.where(Photo.taken_at <= date_to)
    photo_ids = _apply_search_filters(
        photo_ids, db, comment_q, min_rating, near_lat, near_lng, near_radius_deg,
        tag=tag, tag_q=tag_q, text_q=text_q, filename_q=filename_q,
    )
    photo_ids = _apply_face_cluster_filter(photo_ids, db, face_cluster_id)

    # Scope the group-bys to the visible / filtered photo set whenever
    # filters or ACL are in play.
    _scope = _filtered or bool(hidden)

    out: list[TagSummary] = []

    if source is None or source == "user":
        user_q = (
            select(
                Tag.id, Tag.name,
                func.count(PhotoTag.photo_id).label("cnt"),
            )
            .join(PhotoTag, PhotoTag.tag_id == Tag.id)
            .group_by(Tag.id)
        )
        if _scope:
            user_q = user_q.where(PhotoTag.photo_id.in_(photo_ids))
        for r in db.execute(user_q).all():
            out.append(
                TagSummary(id=r.id, name=r.name, count=int(r.cnt or 0), source="user")
            )

    if source is None or source != "user":
        auto_q = (
            select(
                Tag.id, Tag.name, PhotoAutoTag.source,
                func.count(PhotoAutoTag.photo_id).label("cnt"),
            )
            .join(PhotoAutoTag, PhotoAutoTag.tag_id == Tag.id)
            .group_by(Tag.id, PhotoAutoTag.source)
        )
        if source is not None:
            auto_q = auto_q.where(PhotoAutoTag.source == source)
        if _scope:
            auto_q = auto_q.where(PhotoAutoTag.photo_id.in_(photo_ids))
        for r in db.execute(auto_q).all():
            out.append(TagSummary(
                id=r.id, name=r.name,
                count=int(r.cnt or 0), source=r.source or "auto",
            ))

    out.sort(key=lambda x: (-x.count, x.name))
    if not _filtered:
        _TAGS_CACHE[cache_key] = (_time.monotonic(), out)
    return out


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
    include_companion_videos: bool = Query(
        False,
        description=(
            "MUST match the value used on /api/photos. Default False mirrors "
            "the listing default — without this, the histogram counts the "
            "Live-Photo MOV halves but the listing hides them, so the total "
            "drifts ahead of the actual row count and dragging the right-rail "
            "scrubber to the end lands the user on empty pages past the tail."
        ),
    ),
    min_size_kb: int | None = Query(None, ge=0),
    max_size_kb: int | None = Query(None, ge=0),
    comment_q: str | None = None,
    min_rating: int | None = Query(None, ge=1, le=5),
    near_lat: float | None = Query(None, ge=-90, le=90),
    near_lng: float | None = Query(None, ge=-180, le=180),
    near_radius_deg: float | None = Query(None, gt=0, le=10),
    tag: str | None = None,
    tag_q: str | None = None,
    face_cluster_id: int | None = None,
    owner_user_id: int | None = Query(
        None, description="Filter to photos uploaded by this user (0 = no recorded uploader)."
    ),
    user: User = Depends(require_auth),
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
    q = apply_visible_photo_filter(q, db, user)
    q = _apply_scalar_filters(
        q,
        root_id=root_id, path_prefix=path_prefix,
        media_kind=media_kind, include_companion_videos=include_companion_videos,
        min_size_kb=min_size_kb, max_size_kb=max_size_kb,
        no_date_only=no_date_only, date_from=date_from, date_to=date_to,
        owner_user_id=owner_user_id,
    )

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
def list_visible_roots(
    user: User = Depends(require_auth),
    db: Session = Depends(get_db),
) -> list[RootSummary]:
    """Enabled roots with their active-photo counts (for the folder tab).

    Roots the caller has at level=hidden are dropped from the response
    so they don't show up in the folder browser either.
    """
    hidden = hidden_root_ids(db, user)
    q = (
        select(Root, func.count(Photo.id))
        .outerjoin(
            Photo, (Photo.root_id == Root.id) & (Photo.status == "active")
        )
        .where(Root.enabled.is_(True))
        .group_by(Root.id)
        .order_by(Root.label)
    )
    if hidden:
        q = q.where(~Root.id.in_(hidden))
    rows = db.execute(q).all()
    return [
        RootSummary(id=r.id, label=r.label, abs_path=r.abs_path, total_count=cnt or 0)
        for r, cnt in rows
    ]


class UploaderOut(BaseModel):
    id: int           # user_id; 0 means "no recorded uploader" bucket
    username: str
    count: int


@router.get("/uploaders", response_model=list[UploaderOut])
def list_uploaders(
    user: User = Depends(require_auth),
    db: Session = Depends(get_db),
) -> list[UploaderOut]:
    """Distinct uploaders (Photo.owner_user_id) with photo counts, for
    the search filter dropdown. Includes a synthetic id=0 bucket
    counting photos with no recorded uploader (legacy / scanner imports).

    Counts are restricted to photos the caller can see (ACL applied)
    so the dropdown numbers match what the gallery actually shows.
    """
    base = select(Photo.owner_user_id, func.count().label("cnt")).where(
        Photo.status == "active",
    )
    base = apply_visible_photo_filter(base, db, user).group_by(Photo.owner_user_id)
    rows = db.execute(base).all()

    # Resolve usernames in one extra round-trip (users table is tiny).
    counts: dict[int | None, int] = {row[0]: int(row[1] or 0) for row in rows}
    user_ids = [uid for uid in counts if uid is not None]
    name_map: dict[int, str] = {}
    if user_ids:
        for u in db.execute(
            select(User.id, User.username).where(User.id.in_(user_ids))
        ).all():
            name_map[int(u[0])] = str(u[1])

    out: list[UploaderOut] = []
    for uid, cnt in counts.items():
        if uid is None:
            out.append(UploaderOut(id=0, username="(미지정)", count=cnt))
        elif uid in name_map:
            out.append(UploaderOut(id=int(uid), username=name_map[uid], count=cnt))
        else:
            # User was deleted but photos retained the dangling owner_user_id
            # (ondelete=SET NULL was the design, but be defensive).
            out.append(UploaderOut(id=int(uid), username=f"#{uid}", count=cnt))
    # Most-prolific first, "미지정" demoted to the end so the dropdown
    # leads with real uploaders.
    out.sort(key=lambda x: (x.id == 0, -x.count, x.username))
    return out


class FolderChild(BaseModel):
    name: str
    count: int
    has_children: bool


class FolderListing(BaseModel):
    prefix: str
    direct_count: int   # photos directly at this prefix (not in any subfolder)
    children: list[FolderChild]
    # Surfaced so the folder tree can grey out the CRUD menu items
    # when the root is readonly. None means "didn't check" (shouldn't
    # happen for /folders specifically; included for safety).
    root_readonly: bool | None = None


@router.get("/folders", response_model=FolderListing)
def list_folders(
    root_id: int = Query(..., description="which root to walk"),
    prefix: str = Query("", description="rel_path prefix; empty = root of the tree"),
    user: User = Depends(require_auth),
    db: Session = Depends(get_db),
) -> FolderListing:
    """Return the immediate subfolders under (root_id, prefix) with photo counts.

    The folder hierarchy isn't stored as such — we derive it from the
    distinct prefixes of `photos.rel_path`. SQLite uses the
    (root_id, rel_path) unique-index for the LIKE 'prefix%' lookup, so
    walking deep folders is cheap; the top-level call still scans the
    whole root but only returns the first segments.
    """
    # ACL guard — folder-aware so a user with folder_acl re-granting
    # `read` inside a hidden root can still browse that subtree.
    require_folder_level(
        db, user, root_id, (prefix or "").rstrip("/"), "read",
    )

    if prefix and not prefix.endswith("/"):
        prefix = prefix + "/"

    q = select(Photo.rel_path).where(
        Photo.root_id == root_id,
        Photo.status == "active",
    )
    if prefix:
        q = q.where(Photo.rel_path.like(escape_like(prefix) + "%", escape="\\"))

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

    # Surface empty folders too (newly-created ones, or ones the user
    # is about to move photos into) by listdir-ing the filesystem. The
    # DB-derived children take precedence (they already have a count);
    # fs-only folders show count=0 and has_children based on fs walk.
    root = db.get(Root, root_id)
    root_readonly = bool(root.readonly) if root is not None else None
    if root is not None:
        from ..config import get_settings as _gs
        from ..scanner.utils import (
            nfc as _nfc,
            rel_path_is_ignored as _rpi,
            root_ignore_paths as _rip,
        )
        ignore_dirs = set(_gs().scanner.ignore_dirs)
        # User-managed per-root ignore paths — same filtering the gallery
        # uses, so the folder tree doesn't surface "예외 처리된 폴더"
        # as a phantom count=0 leaf via the fs-walk enrichment below.
        user_ignore_paths = _rip(root)
        try:
            target = Path(root.abs_path) / (prefix.rstrip("/") if prefix else "")
            if target.is_dir():
                for entry in os.scandir(target):
                    try:
                        if not entry.is_dir(follow_symlinks=False):
                            continue
                    except OSError:
                        continue
                    name = _nfc(entry.name)
                    if name in ignore_dirs or name.startswith("."):
                        continue
                    # Hide user-ignored subfolders. `prefix` already
                    # ends in "/" when non-empty (see top of the
                    # function), so concatenating yields the full
                    # POSIX rel_path the user typed in.
                    full_rel = (prefix + name).rstrip("/")
                    if _rpi(full_rel, user_ignore_paths):
                        continue
                    if name in children:
                        continue
                    # Peek one level deeper to set has_children.
                    has_kids = False
                    try:
                        for sub in os.scandir(entry.path):
                            try:
                                if sub.is_dir(follow_symlinks=False):
                                    sub_name = _nfc(sub.name)
                                    if sub_name in ignore_dirs or sub_name.startswith("."):
                                        continue
                                    # Also respect user ignore at the
                                    # deeper level so has_children
                                    # doesn't lie when every child is
                                    # ignored.
                                    sub_rel = (full_rel + "/" + sub_name)
                                    if _rpi(sub_rel, user_ignore_paths):
                                        continue
                                    has_kids = True
                                    break
                            except OSError:
                                continue
                    except OSError:
                        pass
                    children[name] = {"count": 0, "has_children": has_kids}
        except (OSError, Exception):
            # Filesystem listing is best-effort enrichment — if it
            # fails (mount glitch, permission flap) we still return
            # the DB-derived children.
            pass

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
        root_readonly=root_readonly,
    )


@router.get("/locations", response_model=list[MarkerOut])
def list_locations(
    bbox: str | None = Query(
        None,
        description="Filter: 'minLng,minLat,maxLng,maxLat'. Omit to return everything.",
    ),
    root_id: int | None = None,
    path_prefix: str | None = None,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
    no_date_only: bool = Query(False),
    text_q: str | None = None,
    filename_q: str | None = None,
    media_kind: str | None = Query(None, pattern="^(image|video)$"),
    min_size_kb: int | None = Query(None, ge=0),
    max_size_kb: int | None = Query(None, ge=0),
    comment_q: str | None = None,
    tag_q: str | None = None,
    tag: str | None = None,
    min_rating: int | None = Query(None, ge=1, le=5),
    face_cluster_id: int | None = None,
    owner_user_id: int | None = Query(
        None, description="Filter to photos uploaded by this user (0 = no recorded uploader)."
    ),
    # Cap bumped to 250k so libraries that have grown past the old 50k cap
    # still see every marker. The payload is lat/lng/id only (~30 bytes per
    # row), so 100k ≈ 3 MB — Leaflet.markercluster handles that fine via
    # chunkedLoading on the frontend.
    limit: int = Query(5000, ge=1, le=250_000),
    user: User = Depends(require_auth),
    db: Session = Depends(get_db),
) -> list[MarkerOut]:
    """Lightweight marker list for the map view. Lat/Lng only.

    Accepts the same filters as list_photos (except near_*, excluded by
    design — clamping the map to a small radius around its own markers
    would be tautological) so the header search and the folder/topic
    sidebar selections also constrain map markers.

    Filters out photos without a thumbnail — otherwise the popup would
    show a broken image and clicking it would 404 from the lightbox.
    """
    q = select(PhotoLocation.photo_id, PhotoLocation.latitude, PhotoLocation.longitude).join(
        Photo, Photo.id == PhotoLocation.photo_id
    ).where(
        Photo.status == "active",
        Photo.thumb_status.in_(("ok", "partial")),
    )
    q = apply_visible_photo_filter(q, db, user)

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
        q = q.where(Photo.rel_path.like(escape_like(path_prefix) + "%", escape="\\"))
    if owner_user_id is not None:
        if owner_user_id == 0:
            q = q.where(Photo.owner_user_id.is_(None))
        else:
            q = q.where(Photo.owner_user_id == owner_user_id)
    if media_kind:
        q = q.where(Photo.media_kind == media_kind)
    if min_size_kb is not None:
        q = q.where(Photo.file_size >= min_size_kb * 1024)
    if max_size_kb is not None:
        q = q.where(Photo.file_size <= max_size_kb * 1024)
    if no_date_only:
        q = q.where(Photo.taken_at.is_(None))
    else:
        if date_from is not None:
            q = q.where(Photo.taken_at >= date_from)
        if date_to is not None:
            q = q.where(Photo.taken_at <= date_to)
    # Search filters (comment / tag / rating / face cluster / text) — near_*
    # excluded on purpose (see docstring above).
    q = _apply_search_filters(
        q, db, comment_q, min_rating, None, None, None,
        tag=tag, tag_q=tag_q, text_q=text_q, filename_q=filename_q,
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
    # ML-generated labels (YOLO / CLIP / face). Separate from `tags` so
    # the editor doesn't accidentally overwrite them on save.
    # Each entry: {"name": str, "source": "auto-yolo"|"auto-clip"|"face",
    # "confidence": float|None}
    auto_tags: list[dict] = []
    # Joined from users by owner_user_id — null when owner_user_id is
    # NULL (legacy / scanner-imported) or when the uploader account
    # has since been deleted.
    owner_username: str | None = None


@router.get("/{photo_id}", response_model=PhotoOut)
def get_photo(
    photo_id: int,
    user: User = Depends(require_auth),
    db: Session = Depends(get_db),
) -> PhotoOut:
    p = db.get(Photo, photo_id)
    if p is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    require_photo_level(db, user, p, "read")
    out = PhotoOut.model_validate(p)
    # Cheap filesystem probe (~2 syscalls) so the lightbox can pre-grey
    # the rotate/delete buttons with a tooltip explaining why instead
    # of letting the user click and only then see a 409.
    root = db.get(Root, p.root_id)
    out.root_readonly = bool(root.readonly) if root is not None else None
    reason = check_write_blocked(p, root)
    out.writable = reason is None
    out.write_block_reason = reason
    return out


class DuplicateOut(BaseModel):
    """Other photo rows whose contents (sha256) match this one.

    Same byte sequence found in multiple folders — e.g. the same JPEG
    archived once under year/month and once under an album. Thumbnails
    are already shared on disk (`data/thumbs/<size>/.../<sha>.jpg`)
    because thumb paths are keyed on sha256, so 'duplicates' here is a
    catalog/UX concern, not a storage one.
    """

    id: int
    root_id: int
    root_label: str
    rel_path: str
    filename: str
    taken_at: datetime | None = None


@router.get("/{photo_id}/duplicates", response_model=list[DuplicateOut])
def list_duplicates(
    photo_id: int,
    user: User = Depends(require_auth),
    db: Session = Depends(get_db),
) -> list[DuplicateOut]:
    """Return active photos that share this one's sha256, excluding itself.
    Empty list if the photo hasn't been hashed yet or has no duplicates.

    Duplicates in hidden roots are filtered out — same ACL rules as the
    rest of the gallery.
    """
    p = db.get(Photo, photo_id)
    if p is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    require_photo_level(db, user, p, "read")
    if not p.sha256:
        return []
    hidden = hidden_root_ids(db, user)
    q = (
        select(
            Photo.id, Photo.root_id, Root.label, Photo.rel_path,
            Photo.filename, Photo.taken_at,
        )
        .join(Root, Root.id == Photo.root_id)
        .where(
            Photo.sha256 == p.sha256,
            Photo.id != p.id,
            Photo.status == "active",
        )
        .order_by(Root.label, Photo.rel_path)
    )
    if hidden:
        q = q.where(~Photo.root_id.in_(hidden))
    rows = db.execute(q).all()
    return [
        DuplicateOut(
            id=r[0], root_id=r[1], root_label=r[2],
            rel_path=r[3], filename=r[4], taken_at=r[5],
        )
        for r in rows
    ]


@router.get("/{photo_id}/details", response_model=PhotoDetail)
def get_photo_details(
    photo_id: int,
    user: User = Depends(require_auth),
    db: Session = Depends(get_db),
) -> PhotoDetail:
    # Single query that eagerly loads photo + its (optional) location +
    # its root, instead of three separate db.get() round-trips. Photo.root
    # / Photo.location are existing relationships; joinedload turns them
    # into LEFT OUTER JOINs.
    from sqlalchemy.orm import joinedload
    p = db.execute(
        select(Photo)
        .options(joinedload(Photo.location), joinedload(Photo.root))
        .where(Photo.id == photo_id)
    ).unique().scalar_one_or_none()
    if p is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    require_photo_level(db, user, p, "read")
    out = PhotoDetail.model_validate(p)
    out.root_readonly = bool(p.root.readonly) if p.root is not None else None
    # Same write-permission probe as get_photo so the lightbox can
    # disable rotate/delete buttons with an explanation tooltip.
    reason = check_write_blocked(p, p.root)
    out.writable = reason is None
    out.write_block_reason = reason
    if p.location is not None:
        out.latitude = p.location.latitude
        out.longitude = p.location.longitude
        out.altitude = p.location.altitude
    # Resolve uploader username (if any) — keep the lookup defensive so
    # a deleted user doesn't break /details.
    if p.owner_user_id is not None:
        out.owner_username = db.execute(
            select(User.username).where(User.id == p.owner_user_id)
        ).scalar_one_or_none()

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
    # `tags` keeps its original meaning (user-applied tags only — the
    # lightbox's chip editor reads/writes this list). ML labels live
    # in `auto_tags` as (name, source[, confidence]) so the UI can
    # render them in a separate, non-editable section.
    try:
        from ..models import PhotoAutoTag
        out.taken_at_original = p.taken_at_original
        out.description = p.description
        tag_rows = db.execute(
            select(Tag.name)
            .join(PhotoTag, PhotoTag.tag_id == Tag.id)
            .where(PhotoTag.photo_id == photo_id)
            .order_by(Tag.name)
        ).all()
        out.tags = [r[0] for r in tag_rows]

        auto_rows = db.execute(
            select(Tag.name, PhotoAutoTag.source, PhotoAutoTag.confidence)
            .join(PhotoAutoTag, PhotoAutoTag.tag_id == Tag.id)
            .where(PhotoAutoTag.photo_id == photo_id)
            .order_by(PhotoAutoTag.source, Tag.name)
        ).all()
        out.auto_tags = [
            {"name": n, "source": s, "confidence": c}
            for (n, s, c) in auto_rows
        ]
    except Exception:
        pass

    return out


# ---- bulk operations (multi-select grid → delete / zip download) ----

class BulkActionIn(BaseModel):
    photo_ids: list[int]


_BULK_LIMIT = 1000
_DOWNLOAD_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{16,48}$")


def trash_photos_core(
    db: Session, photo_ids: list[int], user: User, *, bulk: bool = True,
) -> dict:
    """Move a batch of photos to data/trash/ and mark them trashed.

    Pure service — no FastAPI deps, no permission/ACL checks (caller's
    responsibility, since those are coupled to the request context).
    Used by the bulk_delete endpoint and by the dedup_cleanup worker.

    Commits the session. Returns
    {deleted, failed: [{id, reason}], skipped_readonly: [...], ids: [...]}.
    """
    rows = db.execute(
        select(Photo).where(Photo.id.in_(photo_ids))
    ).scalars().all()
    roots_map = {r.id: r for r in db.execute(select(Root)).scalars().all()}

    deleted: list[int] = []
    failed: list[dict] = []
    skipped_readonly: list[int] = []
    for p in rows:
        root = roots_map.get(p.root_id)
        if root is None:
            failed.append({"id": p.id, "reason": "root not found"})
            continue
        if root.readonly:
            skipped_readonly.append(p.id)
            continue
        # Pre-flight: catch permission problems before launching the
        # actual move, so the caller gets an immediate, accurate reason
        # ("상위 폴더에 쓰기 권한이 없습니다") instead of a generic
        # "이동 실패" 30 seconds later.
        block = check_write_blocked(p, root)
        if block:
            log.info("trash_photos: photo %s pre-flight blocked: %s", p.id, block)
            failed.append({"id": p.id, "reason": block})
            continue
        try:
            result = _move_to_trash(p, root, user)
        except Exception as e:
            log.warning("trash_photos move failed for photo %s: %s", p.id, e)
            failed.append({"id": p.id, "reason": str(e)[:200]})
            continue
        # If the file is still on disk at the original location (move
        # failed e.g. due to directory permissions), DON'T mark the row
        # trashed — otherwise the next scanner pass sees the leftover
        # file and resurrects the photo back to status='active',
        # making it look like delete "didn't take".
        moved = result.get("moved", False) if isinstance(result, dict) else False
        reason = result.get("reason") if isinstance(result, dict) else None
        if not moved and reason and reason != "source file already missing":
            log.warning(
                "trash_photos: photo %s NOT trashed (file still on disk): %s",
                p.id, reason,
            )
            failed.append({"id": p.id, "reason": reason})
            continue
        if p.status != "trashed":
            p.status = "trashed"
        # P5: record who sent it to trash so the trash list can
        # isolate each user's deletions.
        p.trashed_by_user_id = user.id
        deleted.append(p.id)
        audit.record(
            db, user, "photo.trash", "photo", p.id,
            detail={"bulk": bulk, "filename": p.filename, "moved": moved},
        )
    db.commit()
    return {
        "deleted": len(deleted),
        "failed": failed,
        "skipped_readonly": skipped_readonly,
        "ids": deleted,
    }


@router.post("/bulk-delete")
def bulk_delete(
    payload: BulkActionIn,
    user: User = Depends(require_can_delete),
    db: Session = Depends(get_db),
) -> dict:
    """Move every selected photo to data/trash/ in one shot. Requires
    `can_delete` (admin bypasses).

    Photos whose root is readonly are skipped (reported in `skipped_readonly`)
    so a mixed selection still trashes what it can without aborting the
    whole batch.
    """
    if not payload.photo_ids:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "사진을 선택하세요")
    if len(payload.photo_ids) > _BULK_LIMIT:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"한 번에 {_BULK_LIMIT}장까지 가능합니다",
        )

    # ACL guard — every photo must be at level=interact (same as the
    # single-photo delete endpoint). `can_delete` (Depends above) is the
    # real authorization gate; this guard only excludes photos the user
    # has less-than-normal access to (read-only or hidden). Bad apple
    # aborts the whole batch — no partial-delete surprises.
    require_photo_ids_level(db, user, payload.photo_ids, "interact")

    return trash_photos_core(db, payload.photo_ids, user)


class BulkRotateIn(BaseModel):
    photo_ids: list[int]
    # 'cw' = 90° clockwise, 'ccw' = 90° counter-clockwise, '180' = half turn.
    direction: str = Field(pattern=r"^(cw|ccw|180)$")


@router.post("/bulk-rotate")
def bulk_rotate(
    payload: BulkRotateIn,
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
) -> dict:
    """Rotate selected photos *losslessly* by updating the EXIF
    Orientation tag — no pixel re-encoding, no quality loss. Works on
    JPEG / HEIC / TIFF and most RAW formats (CR2, NEF, ARW, DNG, RAF,
    PEF, …) because the change touches metadata only.

    Skips photos on readonly roots and reports per-photo outcomes.
    After the tag is rewritten the worker re-hashes the file and
    regenerates the thumbnail so the gallery reflects the new
    orientation; the original pixel data is byte-identical aside
    from the EXIF block.
    """
    if not payload.photo_ids:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "사진을 선택하세요")
    if len(payload.photo_ids) > _BULK_LIMIT:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"한 번에 {_BULK_LIMIT}장까지 가능합니다",
        )

    # ACL guard — same shape as bulk-delete: every photo must be at
    # level=interact, otherwise abort the whole batch (no partial-rotate
    # surprises). Rotation writes the EXIF Orientation tag on the file
    # so it's admin-only (via require_admin above); this is the
    # photo-level ACL on top.
    require_photo_ids_level(db, user, payload.photo_ids, "interact")

    from ..worker.rotate import (
        next_orientation,
        regenerate_rotated_thumbnails,
        rehash_file,
        rotate_orientation_tag,
    )

    tool = exiftool_path()
    if not tool:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "ExifTool이 설치되어 있지 않아 회전할 수 없습니다",
        )

    rows = db.execute(
        select(Photo).where(Photo.id.in_(payload.photo_ids))
    ).scalars().all()
    roots_map = {r.id: r for r in db.execute(select(Root)).scalars().all()}

    # `rotated` carries the full per-photo result so the client can
    # update the matching grid tile in place (no full applyFilters
    # reload, scroll position preserved). Each entry: id + new sha256
    # (used as the thumb-URL cache buster) + new orientation.
    rotated: list[dict] = []
    failed: list[dict] = []
    skipped_readonly: list[int] = []
    # Videos (MP4/MOV) carry their orientation as a transformation
    # matrix in the moov atom, NOT as the EXIF Orientation tag this
    # endpoint writes. Most video players ignore EXIF Orientation
    # entirely, so writing it would either no-op visually or fail
    # outright for some container formats. Skip videos explicitly and
    # surface the reason to the client so the UI can say
    # "동영상은 회전 미지원" instead of silently doing nothing.
    skipped_video: list[int] = []

    # WARN level so it always lands in journalctl regardless of how
    # the API process's root logger was configured. Drop back to INFO
    # once we know rotation is reliably end-to-end.
    log.warning(
        "bulk-rotate: user=%s direction=%s photo_ids=%s",
        user.username, payload.direction, payload.photo_ids,
    )
    for p in rows:
        root = roots_map.get(p.root_id)
        if root is None:
            log.warning("bulk-rotate: photo %d → root %d missing", p.id, p.root_id)
            failed.append({"id": p.id, "reason": "root not found"})
            continue
        if root.readonly:
            log.warning("bulk-rotate: photo %d skipped (root readonly)", p.id)
            skipped_readonly.append(p.id)
            continue
        if p.media_kind == "video":
            # ExifTool's EXIF Orientation write does not flip the moov-
            # atom matrix that video players honour. Skip cleanly.
            log.info("bulk-rotate: photo %d skipped (video — not supported)", p.id)
            skipped_video.append(p.id)
            continue

        abs_path = join_root(root.abs_path, p.rel_path)
        if not os.path.exists(abs_path):
            log.warning("bulk-rotate: photo %d file missing on disk: %s",
                        p.id, abs_path)
            failed.append({"id": p.id, "reason": "file missing"})
            continue
        # Pre-flight write check — bail before exiftool spends 1-2 s
        # only to fail with "_exiftool_tmp" on a non-writable folder.
        block = check_write_blocked(p, root)
        if block:
            log.info("bulk-rotate: photo %s pre-flight blocked: %s", p.id, block)
            failed.append({"id": p.id, "reason": block})
            continue

        new_orient = next_orientation(p.orientation, payload.direction)
        log.warning(
            "bulk-rotate: photo=%d %s → %s (direction=%s) path=%s",
            p.id, p.orientation, new_orient, payload.direction, abs_path,
        )

        # 1) Rewrite the EXIF Orientation tag via ExifTool. The helper
        #    owns the subprocess invocation + the -overwrite_original
        #    → -overwrite_original_in_place fallback + the -m
        #    ignore-minor-errors handling we learned the hard way.
        write = rotate_orientation_tag(tool, abs_path, new_orient)
        if not write.ok:
            failed.append({"id": p.id, "reason": write.error})
            continue

        # 2) File byte-stream changed (EXIF block rewritten). Recompute
        #    sha256 + signature INLINE so the gallery sees the new value
        #    the moment this API returns — the frontend uses sha256 as
        #    the thumb-URL cache buster.
        try:
            rh = rehash_file(abs_path)
        except OSError as e:
            log.warning("bulk-rotate: photo %d re-hash failed: %s", p.id, e)
            failed.append({"id": p.id, "reason": f"re-hash: {e}"})
            continue
        old_sha = p.sha256
        p.sha256 = rh.sha256
        p.content_signature = rh.content_signature
        p.file_size = rh.file_size
        log.warning(
            "bulk-rotate: photo %d sha %s → %s",
            p.id, (old_sha or "(none)")[:12], rh.sha256[:12],
        )

        # 3) Pixel-rotate the existing thumb at the new sha path. Falls
        #    back to a worker.thumbs.generate() for photos that had no
        #    prior thumb on disk (HEIC quirk explained inside the helper).
        settings = get_settings()
        sizes = sorted(set(settings.thumbnails.sizes))
        quality = getattr(settings.thumbnails, "quality",
                          getattr(settings.thumbnails, "jpeg_quality", 88))
        try:
            tr = regenerate_rotated_thumbnails(
                old_sha, rh.sha256, payload.direction, sizes, quality,
            )
        except Exception:
            log.exception("bulk-rotate: inline thumb regen failed for %s", abs_path)
            p.thumb_status = "pending"
        else:
            if tr.status == "pending":
                # No prior thumb on disk — fall back to the worker's
                # full generate() so the new sha actually has art.
                from ..worker import thumbs as _thumbs_mod
                gen = _thumbs_mod.generate(
                    str(abs_path), rh.sha256, media_kind=p.media_kind,
                )
                p.thumb_status = "pending" if gen.status == "failed" else gen.status
                if gen.error:
                    p.thumb_error = gen.error[:1000]
                log.warning(
                    "bulk-rotate: photo %d no prior thumb → generate() status=%s",
                    p.id, gen.status,
                )
            else:
                p.thumb_status = tr.status
                log.warning(
                    "bulk-rotate: photo %d rotated thumb sizes=%s",
                    p.id, tr.written_sizes,
                )

        p.orientation = new_orient
        # EXIF stays pending so the worker re-extracts every tag (camera
        # info, GPS, etc. didn't change but re-reading is cheap and keeps
        # the row consistent with the file). Worker will skip re-hashing
        # since sha256 is already set.
        p.exif_status = "pending"
        # Priority 12 sits between the daily tick (10) and the
        # admin-triggered scan (20) — finishes the residual re-extract
        # without preempting a running large job.
        from ..worker.jobs import enqueue as _enqueue_job
        _enqueue_job(
            db, kind="index_file", payload={"photo_id": p.id}, priority=12,
        )
        rotated.append({
            "id": p.id,
            "sha256": p.sha256,
            "orientation": new_orient,
        })
        audit.record(
            db, user, "photo.rotate", "photo", p.id,
            detail={"direction": payload.direction, "orientation": new_orient},
        )

    db.commit()
    return {
        "rotated": len(rotated),
        "failed": failed,
        "skipped_readonly": skipped_readonly,
        "skipped_video": skipped_video,
        "items": rotated,
    }


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

    # ACL guard — caller must have at least read on every photo. Hidden
    # roots raise 404 for the offending id; insufficient level raises 403.
    require_photo_ids_level(db, user, payload.photo_ids, "read")

    from ..worker.export import ZipItem, build_photo_zip, sweep_old_downloads

    sweep_old_downloads(TMP_DIR)
    TMP_DIR.mkdir(parents=True, exist_ok=True)

    rows = db.execute(
        select(Photo)
        .where(Photo.id.in_(payload.photo_ids), Photo.status == "active")
    ).scalars().all()
    if not rows:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "유효한 사진이 없습니다")
    roots_map = {r.id: r for r in db.execute(select(Root)).scalars().all()}

    items: list[ZipItem] = []
    pre_skipped: list[int] = []
    for p in rows:
        root = roots_map.get(p.root_id)
        if root is None:
            pre_skipped.append(p.id)
            continue
        items.append(ZipItem(
            photo_id=p.id,
            src_path=Path(join_root(root.abs_path, p.rel_path)),
            suggested_name=p.filename or f"photo_{p.id}",
        ))

    token = secrets.token_urlsafe(18)
    tmp_path = TMP_DIR / f"download_{token}.zip"
    try:
        result = build_photo_zip(items, tmp_path)
    except Exception as e:
        try:
            tmp_path.unlink()
        except OSError:
            pass
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR, f"zip 생성 실패: {e}"
        )

    if result.added == 0:
        try:
            tmp_path.unlink()
        except OSError:
            pass
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, "압축할 수 있는 사진이 없습니다"
        )

    return {
        "url": f"/api/photos/bulk-download/{token}",
        "filename": f"myphotos-{result.added}.zip",
        "added": result.added,
        "skipped": pre_skipped + result.skipped,
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
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
) -> dict:
    """Set or revert the photo's capture time.

    Admin-only (modifies the file). Writes DateTimeOriginal +
    CreateDate (and the iPhone SubSec variants) to the file's EXIF so
    the file is the source of truth; the DB row is then refreshed
    inline. taken_at_original keeps the snapshot of the EXIF value as
    of the first edit so the user can still "원래대로 복원" — and
    revert is itself a file write that puts the snapshot back into the
    EXIF, clearing the snapshot afterward.
    """
    p = db.get(Photo, photo_id)
    if p is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND)

    root = db.get(Root, p.root_id)
    block = check_write_blocked(p, root)
    if block:
        raise HTTPException(status.HTTP_409_CONFLICT, block)

    abs_path = join_root(root.abs_path, p.rel_path)
    tool = exiftool_path()
    if not tool:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "ExifTool 미설치 — vendor 디렉토리 또는 시스템 경로에 설치하세요",
        )

    from ..worker import exif_write

    if payload.taken_at is None:
        # Revert: only meaningful if we snapshotted an original.
        if p.taken_at_original is None:
            return {
                "ok": True,
                "taken_at": p.taken_at.isoformat() if p.taken_at else None,
                "taken_at_original": None,
            }
        target = p.taken_at_original
        result = exif_write.write_taken_at(tool, abs_path, target)
        if not result.ok:
            raise HTTPException(
                status.HTTP_500_INTERNAL_SERVER_ERROR,
                f"EXIF 촬영시각 복원 실패: {result.error}",
            )
        p.taken_at = target
        p.taken_at_original = None
    else:
        # First edit snapshots the EXIF value (whatever's in p.taken_at
        # now) so a future revert has somewhere to go back to.
        snapshot = (p.taken_at_original is None and p.taken_at is not None)
        result = exif_write.write_taken_at(tool, abs_path, payload.taken_at)
        if not result.ok:
            raise HTTPException(
                status.HTTP_500_INTERNAL_SERVER_ERROR,
                f"EXIF 촬영시각 쓰기 실패: {result.error}",
            )
        if snapshot:
            p.taken_at_original = p.taken_at
        p.taken_at = payload.taken_at

    _refresh_file_signature(p, abs_path)
    db.commit()
    return {
        "ok": True,
        "taken_at": p.taken_at.isoformat() if p.taken_at else None,
        "taken_at_original": p.taken_at_original.isoformat() if p.taken_at_original else None,
    }


class GpsIn(BaseModel):
    """Manual GPS edit. Both latitude/longitude null = clear; otherwise
    both must be set. Altitude is optional and not editable from the UI
    today but kept here so a future altimeter-aware client can use it."""

    latitude: float | None = Field(default=None, ge=-90, le=90)
    longitude: float | None = Field(default=None, ge=-180, le=180)
    altitude: float | None = None


def _refresh_file_signature(p: Photo, abs_path: str) -> None:
    """ExifTool mutated the file, so its mtime + size + content_signature
    changed too. Refresh the row inline so the scanner's next sweep
    won't see the file as externally modified (and thus won't bounce
    exif_status back to pending and re-extract from the same file we
    just wrote)."""
    try:
        st = os.stat(abs_path)
        p.file_size = st.st_size
        p.mtime = datetime.fromtimestamp(st.st_mtime)
        p.content_signature = f"{st.st_size}:{st.st_mtime_ns}"
    except OSError:
        # File vanished between write and stat — very unusual. Leave
        # the old signature in place; scanner will pick up the
        # mismatch on its next pass.
        log.warning("post-write stat failed for %s", abs_path)


@router.put("/{photo_id}/gps")
def set_gps(
    photo_id: int,
    payload: GpsIn,
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
) -> dict:
    """Set or clear a photo's GPS location, writing both to the file's
    own EXIF (admin-only — modifies the original) AND to the
    photo_locations table.

    Source of truth is the file; the DB row is a cache. Doing both
    inline means the gallery sees the new GPS immediately AND a
    re-index can't roll back the edit (because the file's GPS now
    matches the DB).
    """
    p = db.get(Photo, photo_id)
    if p is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    # Half-set rejection before the writability check so a bad payload
    # gives a clean 400 instead of failing at the file layer.
    if (payload.latitude is None) != (payload.longitude is None):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "latitude and longitude must both be set (or both null to clear)",
        )

    root = db.get(Root, p.root_id)
    block = check_write_blocked(p, root)
    if block:
        raise HTTPException(status.HTTP_409_CONFLICT, block)

    abs_path = join_root(root.abs_path, p.rel_path)
    tool = exiftool_path()
    if not tool:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "ExifTool 미설치 — vendor 디렉토리 또는 시스템 경로에 설치하세요",
        )

    from ..worker import exif_write

    # Clear: wipe every GPS tag from the file, then drop the
    # photo_locations row.
    if payload.latitude is None:
        result = exif_write.clear_gps(tool, abs_path)
        if not result.ok:
            raise HTTPException(
                status.HTTP_500_INTERNAL_SERVER_ERROR,
                f"EXIF GPS 삭제 실패: {result.error}",
            )
        loc = db.get(PhotoLocation, photo_id)
        if loc is not None:
            db.delete(loc)
        _refresh_file_signature(p, abs_path)
        db.commit()
        return {"ok": True, "latitude": None, "longitude": None, "altitude": None}

    # Set: write to file first, then mirror to DB.
    result = exif_write.write_gps(
        tool, abs_path, payload.latitude, payload.longitude, payload.altitude,
    )
    if not result.ok:
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            f"EXIF GPS 쓰기 실패: {result.error}",
        )

    loc = db.get(PhotoLocation, photo_id)
    if loc is None:
        loc = PhotoLocation(
            photo_id=photo_id,
            latitude=payload.latitude,
            longitude=payload.longitude,
            altitude=payload.altitude,
        )
        db.add(loc)
    else:
        loc.latitude = payload.latitude
        loc.longitude = payload.longitude
        loc.altitude = payload.altitude
    _refresh_file_signature(p, abs_path)
    db.commit()
    return {
        "ok": True,
        "latitude": payload.latitude,
        "longitude": payload.longitude,
        "altitude": payload.altitude,
    }


class DescriptionIn(BaseModel):
    description: str | None = None


@router.put("/{photo_id}/description")
def set_description(
    photo_id: int,
    payload: DescriptionIn,
    user: User = Depends(require_can_edit_meta_others),
    db: Session = Depends(get_db),
) -> dict:
    p = db.get(Photo, photo_id)
    if p is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    require_photo_level(db, user, p, "contribute")
    body = (payload.description or "").strip()
    p.description = body or None
    db.commit()
    from .. import fts as _fts
    _fts.rebuild_photo(db, photo_id)
    db.commit()
    return {"ok": True, "description": p.description}


class TagsIn(BaseModel):
    tags: list[str]


@router.put("/{photo_id}/tags", response_model=list[str])
def set_photo_tags(
    photo_id: int,
    payload: TagsIn,
    user: User = Depends(require_can_edit_meta_others),
    db: Session = Depends(get_db),
) -> list[str]:
    p = db.get(Photo, photo_id)
    if p is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    require_photo_level(db, user, p, "contribute")

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

    # Replace the user's tag set atomically. photo_auto_tags lives in
    # its own table now and is untouched here — ML labels stay put
    # even when the user re-saves their tags.
    from sqlalchemy import delete as _delete
    db.execute(_delete(PhotoTag).where(PhotoTag.photo_id == photo_id))
    for tid in tag_ids:
        db.add(PhotoTag(photo_id=photo_id, tag_id=tid))
    db.commit()
    from .. import fts as _fts
    _fts.rebuild_photo(db, photo_id)
    db.commit()

    # Return the resolved names ordered alphabetically for stable UI.
    final = db.execute(
        select(Tag.name)
        .join(PhotoTag, PhotoTag.tag_id == Tag.id)
        .where(PhotoTag.photo_id == photo_id)
        .order_by(Tag.name)
    ).all()
    return [r[0] for r in final]


# ---- visibility (P4) ----

class VisibilityIn(BaseModel):
    visibility: str = Field(pattern=r"^(inherit|private|public)$")


@router.put("/{photo_id}/visibility")
def set_visibility(
    photo_id: int,
    payload: VisibilityIn,
    user: User = Depends(require_auth),
    db: Session = Depends(get_db),
) -> dict:
    """Toggle per-photo visibility (private / public / inherit).

    Authorization model:
    - admin can always change any photo
    - the owner (photo.owner_user_id) can change their own photo
    - everyone else needs at least `manage` level on the photo
      (i.e. an admin gave them root/folder manage)

    Owner-less photos (uploaded before P4) are admin-only for
    visibility changes — there's nobody else who can claim "mine".
    """
    p = db.get(Photo, photo_id)
    if p is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    # Must at least be able to *see* the photo before changing it.
    # require_photo_level handles hidden→404 + level<min→403.
    require_photo_level(db, user, p, "read")

    if not user.is_admin:
        is_owner = (p.owner_user_id is not None and p.owner_user_id == user.id)
        if not is_owner:
            # Non-owner non-admin → needs manage on the photo.
            require_photo_level(db, user, p, "manage")

    before = p.visibility
    p.visibility = payload.visibility
    audit.record(
        db, user, "photo.visibility", "photo", p.id,
        detail={"filename": p.filename, "before": before, "after": p.visibility},
    )
    db.commit()
    return {"ok": True, "visibility": p.visibility}


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
    p = db.get(Photo, photo_id)
    if p is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    require_photo_level(db, user, p, "interact")
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
    p = db.get(Photo, photo_id)
    if p is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    require_photo_level(db, user, p, "interact")
    body = (payload.body or "").strip()
    if not body:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "댓글 내용이 비어있습니다")
    if len(body) > 2000:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "댓글은 2000자 이하만 가능합니다")
    c = PhotoComment(photo_id=photo_id, user_id=user.id, body=body)
    db.add(c)
    db.commit()
    db.refresh(c)
    from .. import fts as _fts
    _fts.rebuild_photo(db, photo_id)
    db.commit()
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
    # Photo must still be visible to the user — if ACL changed since
    # they posted the comment, treat it as if the comment is gone too.
    photo = db.get(Photo, photo_id)
    if photo is not None:
        require_photo_level(db, user, photo, "read")
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
    from .. import fts as _fts
    _fts.rebuild_photo(db, photo_id)
    db.commit()
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
    photo = db.get(Photo, photo_id)
    if photo is not None:
        require_photo_level(db, user, photo, "read")
    if c.user_id != user.id and not user.is_admin:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "본인 댓글만 삭제 가능")
    db.delete(c)
    db.commit()
    from .. import fts as _fts
    _fts.rebuild_photo(db, photo_id)
    db.commit()


@router.get("/{photo_id}/thumb")
def get_thumb(
    photo_id: int,
    size: int = Query(256),
    user: User = Depends(require_auth),
    db: Session = Depends(get_db),
) -> FileResponse:
    p = db.get(Photo, photo_id)
    if p is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    require_photo_level(db, user, p, "read")
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
    # Thumb path is keyed on photo sha256 → contents at this URL never
    # change. immutable + 1y max-age means browser/proxy serves from
    # cache after the first hit; second-page scrolls and lightbox
    # prev/next don't re-pull from the NAS at all.
    return FileResponse(
        path,
        media_type="image/jpeg",
        headers={"Cache-Control": "public, max-age=31536000, immutable"},
    )


@router.get("/{photo_id}/original")
def get_original(
    photo_id: int,
    user: User = Depends(require_auth),
    db: Session = Depends(get_db),
) -> FileResponse:
    """Serve the original file inline so browsers can display JPG/PNG/HEIC in a tab."""
    p = db.get(Photo, photo_id)
    if p is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    require_photo_level(db, user, p, "read")
    root = db.get(Root, p.root_id)
    if root is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "root missing")
    src = Path(join_root(root.abs_path, p.rel_path))
    if not src.exists():
        raise HTTPException(status.HTTP_404_NOT_FOUND, "file missing on disk")
    media_type = mimetypes.guess_type(p.filename)[0] or "application/octet-stream"
    # FileResponse with filename= forces attachment; build the disposition manually.
    # Same immutable trick as the thumb: if the file on disk changes,
    # the next scan re-hashes and ETag would change, but for now the URL
    # itself is keyed on photo_id and the file at that path doesn't get
    # rewritten in place by the catalog (readonly roots especially).
    return FileResponse(
        src,
        media_type=media_type,
        headers={
            "Content-Disposition": f'inline; filename="{p.filename}"',
            "Cache-Control": "public, max-age=31536000, immutable",
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
    user: User = Depends(require_auth),
    db: Session = Depends(get_db),
):
    """Force-download endpoint. `format=png` converts RAW/HEIC/etc. to PNG on the fly."""
    p = db.get(Photo, photo_id)
    if p is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    require_photo_level(db, user, p, "read")
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
    # Decompression-bomb guard. Pillow's default
    # MAX_IMAGE_PIXELS (~89 MP) already raises Image.DecompressionBombError
    # but only WARN-level by default — bump it to a hard error and floor
    # the limit at 64 MP, which is plenty for any consumer camera output
    # (≈9504×6336 = 60 MP for 60 MP cameras) and well below the
    # gigapixel territory needed for an OOM attack.
    Image.MAX_IMAGE_PIXELS = 64_000_000

    img = _read_image_any(src, ext)
    if img is None:
        raise HTTPException(
            status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            "이미지를 디코딩할 수 없습니다 (지원하지 않는 형식)",
        )
    try:
        # Second-line check on the decoded image, in case the loader
        # opened a header that lies about dimensions. width*height is
        # also a useful refuse-threshold even when MAX_IMAGE_PIXELS
        # has already warned.
        if (img.width or 0) * (img.height or 0) > Image.MAX_IMAGE_PIXELS:
            raise HTTPException(
                status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                f"이미지가 너무 큽니다 ({img.width}x{img.height}) — PNG 변환 거부",
            )
        img = ImageOps.exif_transpose(img)
        if img.mode != "RGB":
            img = img.convert("RGB")
        buf = BytesIO()
        img.save(buf, format="PNG", optimize=False)
    except Image.DecompressionBombError:
        raise HTTPException(
            status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            "이미지가 너무 큽니다 (decompression bomb 의심) — PNG 변환 거부",
        )
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


# Refuse to move into the trash if doing so would leave less than this
# many bytes free on the trash volume. 1 GiB headroom keeps the OS / DB
# / running services from running out of space — corruption from that
# is far worse than refusing one delete and asking the user to purge.
_TRASH_MIN_FREE_BYTES = 1 * 1024 * 1024 * 1024


def _check_trash_space(needed_bytes: int) -> str | None:
    """Return a Korean error message when adding `needed_bytes` to
    TRASH_DIR would push free space below _TRASH_MIN_FREE_BYTES, or
    None when the move is safe.
    """
    try:
        usage = shutil.disk_usage(TRASH_DIR)
    except OSError:
        # disk_usage failed (e.g. trash on a remote mount that just
        # dropped) — don't block; the move attempt will surface the
        # real OS error.
        return None
    after_free = usage.free - max(0, needed_bytes)
    if after_free < _TRASH_MIN_FREE_BYTES:
        mb_needed = max(1, needed_bytes // (1024 * 1024))
        mb_free = max(0, usage.free // (1024 * 1024))
        return (
            f"휴지통 디스크 공간 부족: 이 사진 {mb_needed}MB / 여유 "
            f"{mb_free}MB · 안전 여유 1GB 미달. 휴지통의 옛 항목을 "
            "영구 삭제해 공간을 확보한 뒤 다시 시도하세요."
        )
    return None


def check_write_blocked(photo: Photo, root: Root | None) -> str | None:
    """Return None if write ops (rotate / trash) can succeed for this
    photo, else a short Korean reason. Used both as a pre-flight in the
    bulk endpoints (skip with a clean reason instead of waiting for
    exiftool/shutil to fail) and to populate Photo*.writable so the
    lightbox can grey out the buttons up-front.

    Checks layered cheapest → strictest:
      1. root.readonly flag (admin policy)
      2. source file exists on disk
      3. parent directory writable by the API process (= can create
         exiftool's `<file>_exiftool_tmp` AND remove the file entry
         during a trash move)
      4. source file writable (in-place EXIF rewrites need this when
         exiftool's atomic-temp path fails over to `-in_place`)
    """
    if root is None:
        return "사진 폴더(root) 정보가 없습니다"
    if root.readonly:
        return "이 폴더는 읽기 전용 모드입니다 — 관리 → 사진 폴더에서 해제"
    src = Path(join_root(root.abs_path, photo.rel_path))
    if not src.exists():
        return "원본 파일이 디스크에 없습니다"
    parent = src.parent
    if not os.access(str(parent), os.W_OK):
        return (
            f"상위 폴더에 쓰기 권한이 없습니다 ({parent.name}/) — "
            "scripts/fix-photo-perms.sh 또는 README 9단계 참고"
        )
    if not os.access(str(src), os.W_OK):
        return f"파일에 쓰기 권한이 없습니다 ({src.name})"
    return None


def _move_to_trash(p: Photo, root: Root, user: User | None) -> dict:
    """Move the original file from its root into data/trash/<photo_id>/.

    Writes a `_meta.json` sidecar with enough info to manually restore the
    file later. Returns a dict describing the outcome.
    """
    src = Path(join_root(root.abs_path, p.rel_path))
    if not src.exists():
        return {"moved": False, "reason": "source file already missing"}

    # Disk-space guard — refuse the move if the trash volume would
    # drop below the safety margin afterward.
    try:
        size = p.file_size or src.stat().st_size
    except OSError:
        size = 0
    space_err = _check_trash_space(size)
    if space_err:
        return {"moved": False, "reason": space_err}

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
    user: User = Depends(require_can_delete),
) -> dict:
    """Move the original file to data/trash/ and mark the row as trashed.

    Requires `can_delete` (admin always passes). The DB row stays so the
    deletion is recoverable: restoring is a matter of moving the file
    back and
    flipping status to 'active'.
    """
    p = db.get(Photo, photo_id)
    if p is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    # `can_delete` (caller-level Depends) is the real authorization gate.
    # `require_photo_level("interact")` only blocks deletion on photos
    # the user has *less than* normal access to (read-only or hidden) —
    # the ACL doesn't need an explicit "manage" grant for the can_delete
    # bit to be useful, which would defeat the per-user toggle.
    require_photo_level(db, user, p, "interact")
    root = db.get(Root, p.root_id)
    if root is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "root missing")

    if root.readonly:
        # Refuse so the admin who turned on read-only protection can rely
        # on it. Flip the root's readonly flag in the admin UI first if
        # this deletion is intentional.
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"폴더 '{root.label}'(이)가 읽기 전용 모드입니다. "
            "관리 → 사진 폴더에서 readonly를 풀고 다시 시도하세요.",
        )
    # Pre-flight write check — refuse with a clear reason BEFORE
    # attempting the move, so the user sees "상위 폴더에 쓰기 권한이
    # 없습니다 …" instead of a generic 409 from the post-move check.
    block = check_write_blocked(p, root)
    if block:
        raise HTTPException(status.HTTP_409_CONFLICT, block)

    result = _move_to_trash(p, root, user)
    moved = result.get("moved", False)
    reason = result.get("reason")
    # If the file is still on disk (move failed for any reason other
    # than "already missing"), refuse to mark the row trashed — the
    # scanner would resurrect it on the next pass otherwise and the
    # user would see the photo reappear after every refresh. Surface
    # the underlying reason so the UI can show what to fix (typically
    # folder write permissions).
    if not moved and reason and reason != "source file already missing":
        log.warning(
            "delete_photo: photo %s NOT trashed (file still on disk): %s",
            p.id, reason,
        )
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"파일을 휴지통으로 옮기지 못했습니다: {reason}",
        )
    if p.status != "trashed":
        p.status = "trashed"
    # P5: track the deleter (idempotent on re-deletion of an already-
    # trashed row).
    p.trashed_by_user_id = user.id
    audit.record(
        db, user, "photo.trash", "photo", p.id,
        detail={"filename": p.filename, "moved": moved},
    )
    db.commit()
    return {
        "ok": True,
        "id": photo_id,
        "status": p.status,
        "file_moved": moved,
        "reason": reason,
    }

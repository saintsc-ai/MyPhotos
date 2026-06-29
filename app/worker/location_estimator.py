"""GPS estimation for photos that don't carry their own coordinates.

Use case: a DJI Pocket 3 / older camcorder / point-and-shoot drops
images and clips into the same folder as the phone shots from the
same trip. The phone has GPS, the other camera doesn't. Instead of
making the user pick coordinates by hand for every clip, infer them
from the time-nearest neighbour that DOES have GPS in the same (or,
failing that, the parent) folder.

Algorithm (per photo):

  1. Scope = same folder as the target photo (parent of rel_path).
  2. Anchors = photos in scope with a non-estimated location whose
     taken_at is within `threshold_seconds` of the target's taken_at.
  3. If no anchors, scope = one level up, retry. Stop at root.
  4. Pick the nearest anchor before and after the target. If both
     exist, linearly interpolate lat/lng on the time ratio. If only
     one exists, snap to it.
  5. Write a photo_locations row with source='estimated' and
     estimated_from_photo_ids = JSON list of the anchor ids the
     coordinates were derived from.

`exif` and `user` anchors qualify; previously-estimated rows do NOT
(otherwise estimates would propagate noisily, turning each pass into
a smearing operation).

threshold_seconds defaults to 6h — covers an ordinary day-of-travel
session, drops out of bounds when the next GPS anchor is from the
next day.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Iterable, Optional

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from ..models import Photo, PhotoLocation

log = logging.getLogger(__name__)


DEFAULT_THRESHOLD_SECONDS = 6 * 60 * 60          # 6 hours
# Stop walking up the directory tree at this many levels. Pathologically
# deep trees (one user reported `/photo/year/month/day/event/sub/...`)
# could otherwise grind through too many candidate scans per photo. Six
# levels covers every real-world folder layout I've seen.
MAX_PARENT_LEVELS = 6

# Time-window ladder. estimate_for_photo tries each step in order at
# each parent level — closest-in-time anchor wins, but we widen the
# window before giving up so a multi-day trip can still pull GPS off
# day-one phone shots for day-three GPS-less DJI footage. Steps above
# the caller's max are dropped (e.g. max=24h → [6h, 12h, 24h]).
_DEFAULT_THRESHOLD_LADDER_SECONDS = [
    6 * 60 * 60,          # 6 h   — same session
    12 * 60 * 60,         # 12 h  — same day, UTC↔local edge cases
    24 * 60 * 60,         # 24 h  — adjacent days
    72 * 60 * 60,         # 72 h  — short trip start ↔ end
    7 * 24 * 60 * 60,     # 7 d   — week-long trip
]


def _expand_threshold_steps(max_seconds: int) -> list[int]:
    """Return the ladder rungs that don't exceed `max_seconds`. Ensures
    `max_seconds` is the last rung even when it doesn't match a preset
    so a caller passing 36h gets [6h, 12h, 24h, 36h] not just [6h, 12h,
    24h]."""
    steps = [s for s in _DEFAULT_THRESHOLD_LADDER_SECONDS if s < max_seconds]
    steps.append(max_seconds)
    # De-dupe + preserve order in case max_seconds matched a preset.
    seen: set[int] = set()
    out: list[int] = []
    for s in steps:
        if s not in seen:
            seen.add(s)
            out.append(s)
    return out


@dataclass
class EstimateResult:
    latitude: float
    longitude: float
    altitude: Optional[float]
    anchor_ids: list[int]                # 1 or 2 photo ids


def _parent(rel_path: str) -> str:
    """POSIX dirname — empty string when at the root of the photo tree."""
    return rel_path.rsplit("/", 1)[0] if "/" in rel_path else ""


def _scope_like(parent: str) -> str:
    """SQL LIKE pattern matching rel_paths that live directly inside or
    below `parent`. Empty parent → match every row under the root."""
    return (parent + "/%") if parent else "%"


def estimate_for_photo(
    db: Session,
    photo: Photo,
    *,
    threshold_seconds: int = DEFAULT_THRESHOLD_SECONDS,
) -> Optional[EstimateResult]:
    """Try to infer GPS for one photo. Returns None when no anchor in
    range is reachable — the caller should NOT write a row in that
    case so a later pass with a wider scope can still pick it up.

    Algorithm walks two axes:
      - parent levels (same folder → parent → grandparent → ...)
      - time-window ladder (6h → 12h → 24h → ... → threshold_seconds)
    At every parent level the windows widen until either anchors
    appear or the ladder runs out, *then* we walk up. That order
    prefers a same-folder/-trip anchor even if the timestamp is days
    apart, over a different-folder anchor that happens to be hours
    apart — closer in space beats closer in time when the photo is
    organised by trip.

    Read-only — this function does NOT touch the DB; the caller is
    responsible for upserting the photo_locations row + committing.
    """
    if photo.taken_at is None:
        return None
    parent = _parent(photo.rel_path)
    target_ts = photo.taken_at.timestamp()
    steps = _expand_threshold_steps(int(threshold_seconds))

    for _ in range(MAX_PARENT_LEVELS):
        for step in steps:
            anchors = _candidates(db, photo, parent, target_ts, step)
            if anchors:
                return _pick(anchors, target_ts)
        if not parent:
            return None                          # already at root, give up
        parent = _parent(parent)

    return None


def _candidates(
    db: Session,
    photo: Photo,
    scope: str,
    target_ts: float,
    threshold_seconds: int,
) -> list[dict]:
    """Anchor rows from photo_locations whose photo lives in `scope` and
    whose taken_at is within the threshold of `target_ts`. Excludes
    `estimated` rows (we don't want estimates to snowball) and the
    target itself."""
    like = _scope_like(scope)
    rows = db.execute(
        text(
            """
            SELECT
                p.id              AS id,
                p.taken_at        AS taken_at,
                l.latitude        AS lat,
                l.longitude       AS lng,
                l.altitude        AS alt
            FROM photos p
            JOIN photo_locations l ON l.photo_id = p.id
            WHERE p.root_id = :root_id
              AND p.id != :pid
              AND p.taken_at IS NOT NULL
              AND p.rel_path LIKE :like
              AND (l.source IS NULL OR l.source IN ('exif', 'user'))
            """
        ),
        {"root_id": photo.root_id, "pid": photo.id, "like": like},
    ).mappings().all()

    out: list[dict] = []
    for r in rows:
        ts = r["taken_at"]
        if ts is None:
            continue
        if isinstance(ts, str):
            try:
                ts = datetime.fromisoformat(ts)
            except ValueError:
                continue
        ts_s = ts.timestamp()
        if abs(ts_s - target_ts) <= threshold_seconds:
            out.append({
                "id": r["id"], "ts": ts_s,
                "lat": r["lat"], "lng": r["lng"], "alt": r["alt"],
            })
    return out


def _pick(candidates: list[dict], target_ts: float) -> EstimateResult:
    before: Optional[dict] = None
    after: Optional[dict] = None
    for c in candidates:
        if c["ts"] <= target_ts:
            if before is None or c["ts"] > before["ts"]:
                before = c
        if c["ts"] >= target_ts:
            if after is None or c["ts"] < after["ts"]:
                after = c

    if before is not None and after is not None and before["id"] != after["id"]:
        span = after["ts"] - before["ts"]
        # Defensive: span==0 means two anchors at the same instant
        # bracketing the target — equally valid sources, take the
        # mean. Avoids a divide-by-zero.
        t = (target_ts - before["ts"]) / span if span > 0 else 0.5
        lat = before["lat"] + (after["lat"] - before["lat"]) * t
        lng = before["lng"] + (after["lng"] - before["lng"]) * t
        alt = None
        if before.get("alt") is not None and after.get("alt") is not None:
            alt = before["alt"] + (after["alt"] - before["alt"]) * t
        return EstimateResult(
            latitude=lat, longitude=lng, altitude=alt,
            anchor_ids=[before["id"], after["id"]],
        )

    src = before or after
    if src is None:
        # Shouldn't happen — the caller already filtered by threshold —
        # but be defensive.
        return EstimateResult(0.0, 0.0, None, [])
    return EstimateResult(
        latitude=src["lat"], longitude=src["lng"], altitude=src.get("alt"),
        anchor_ids=[src["id"]],
    )


def apply_estimate(
    db: Session, photo: Photo, est: EstimateResult,
) -> None:
    """Upsert the photo_locations row with source='estimated'. Caller
    commits."""
    existing = db.get(PhotoLocation, photo.id)
    if existing is None:
        db.add(PhotoLocation(
            photo_id=photo.id,
            latitude=est.latitude,
            longitude=est.longitude,
            altitude=est.altitude,
            source="estimated",
            estimated_from_photo_ids=json.dumps(est.anchor_ids),
            estimated_at=datetime.utcnow(),
        ))
    else:
        # Only overwrite existing estimates — never clobber an exif or user
        # row, even if the caller asked us to. NULL source = legacy 'exif'
        # (the only kind before the source column), so it's protected too;
        # only an explicitly 'estimated' row may be re-estimated.
        if existing.source != "estimated":
            return
        existing.latitude = est.latitude
        existing.longitude = est.longitude
        existing.altitude = est.altitude
        existing.source = "estimated"
        existing.estimated_from_photo_ids = json.dumps(est.anchor_ids)
        existing.estimated_at = datetime.utcnow()


def estimate_for_root(
    db: Session,
    root_id: int,
    *,
    threshold_seconds: int = DEFAULT_THRESHOLD_SECONDS,
    photo_ids: Optional[Iterable[int]] = None,
    commit_every: int = 500,
) -> dict[str, int]:
    """Run estimation across every GPS-less photo in a root. Commits in
    batches so a long run doesn't hold a single fat transaction (which
    would starve the api/web writers and bloat the WAL).

    Returns {scanned, updated, no_anchor}.
    """
    base_q = (
        select(Photo)
        .outerjoin(PhotoLocation, PhotoLocation.photo_id == Photo.id)
        .where(
            Photo.root_id == root_id,
            Photo.taken_at.is_not(None),
            # Only consider photos whose EXIF pass is settled — that's
            # what populates taken_at + any GPS reading the estimator
            # joins against. Using exif_status rather than thumb_status
            # is intentional: large videos (DJI 4K clips at 50–500 MB)
            # take a long time to thumbnail and sometimes never settle
            # at 'ok', so a thumb-based filter would silently exclude
            # exactly the GPS-less subjects we most want to estimate.
            Photo.exif_status.in_(("ok", "partial")),
            # Target = no location at all, OR an existing 'estimated'
            # row we're allowed to re-derive. Skip exif/user rows.
            (PhotoLocation.photo_id.is_(None))
            | (PhotoLocation.source == "estimated"),
        )
    )
    if photo_ids is not None:
        base_q = base_q.where(Photo.id.in_(list(photo_ids)))
    photos = db.execute(base_q).scalars().all()

    scanned = updated = no_anchor = 0
    pending = 0
    for p in photos:
        scanned += 1
        est = estimate_for_photo(db, p, threshold_seconds=threshold_seconds)
        if est is None:
            no_anchor += 1
            continue
        apply_estimate(db, p, est)
        updated += 1
        pending += 1
        if pending >= commit_every:
            db.commit()
            pending = 0
    if pending:
        db.commit()
    log.info(
        "location estimate: root=%d scanned=%d updated=%d no_anchor=%d",
        root_id, scanned, updated, no_anchor,
    )
    return {"scanned": scanned, "updated": updated, "no_anchor": no_anchor}

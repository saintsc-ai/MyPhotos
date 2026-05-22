"""EXIF extraction with a fallback chain.

Strategy:
  1. Try Pillow first — fast, covers ~90% of consumer files.
  2. If required fields (e.g. taken_at) are still missing, try ExifTool.
     ExifTool reads Pentax MakerNote, HEIC/HEIF boxes, RAW formats, etc.
  3. Whichever extractor produced the final taken_at gets recorded in
     `exif_extractor` so we can audit later ("how many photos still
     need ExifTool to be readable?").

A row is only marked 'failed' when no required field could be recovered;
partial success keeps whatever was extracted.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from PIL import ExifTags, Image

from ..config import get_settings
from . import exiftool_pool

log = logging.getLogger(__name__)

_EXIF_DT_FORMATS = (
    "%Y:%m:%d %H:%M:%S",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%d %H:%M:%S",
)


@dataclass
class ExifResult:
    taken_at: datetime | None = None
    width: int | None = None
    height: int | None = None
    camera_make: str | None = None
    camera_model: str | None = None
    lens: str | None = None
    iso: int | None = None
    fnumber: float | None = None
    exposure: str | None = None
    focal_length: float | None = None
    orientation: int | None = None
    latitude: float | None = None
    longitude: float | None = None
    altitude: float | None = None
    duration_seconds: float | None = None

    extractor: str | None = None
    status: str = "pending"  # ok | partial | failed
    error: str | None = None

    raw: dict[str, Any] = field(default_factory=dict)


def _parse_dt(s: Any) -> datetime | None:
    if not isinstance(s, str):
        return None
    s = s.strip()
    if not s or s.startswith("0000"):
        return None
    for fmt in _EXIF_DT_FORMATS:
        try:
            return datetime.strptime(s[:19], fmt)
        except ValueError:
            continue
    return None


def _gps_to_float(coord: Any, ref: str | None) -> float | None:
    """Convert EXIF GPS rationals (d, m, s) to a signed float."""
    if coord is None:
        return None
    try:
        d, m, s = (float(x) for x in coord)
    except (TypeError, ValueError):
        return None
    val = d + m / 60.0 + s / 3600.0
    if ref in ("S", "W"):
        val = -val
    return val


def _extract_pillow(path: str) -> ExifResult:
    res = ExifResult(extractor="pillow")
    try:
        with Image.open(path) as im:
            res.width, res.height = im.size
            exif = im.getexif() or {}
    except Exception as e:
        res.status = "failed"
        res.error = f"pillow: {e}"
        return res

    if not exif:
        res.status = "partial"  # dims still useful
        return res

    tag_map = {v: k for k, v in ExifTags.TAGS.items()}
    g = lambda name: exif.get(tag_map.get(name))

    res.taken_at = _parse_dt(g("DateTimeOriginal")) or _parse_dt(g("DateTime"))
    res.camera_make = (g("Make") or None) and str(g("Make")).strip() or None
    res.camera_model = (g("Model") or None) and str(g("Model")).strip() or None
    res.lens = (g("LensModel") or None) and str(g("LensModel")).strip() or None
    res.orientation = int(g("Orientation")) if g("Orientation") else None
    res.iso = int(g("ISOSpeedRatings")) if g("ISOSpeedRatings") else None
    try:
        res.fnumber = float(g("FNumber")) if g("FNumber") else None
    except (TypeError, ValueError):
        pass
    try:
        res.focal_length = float(g("FocalLength")) if g("FocalLength") else None
    except (TypeError, ValueError):
        pass
    exp = g("ExposureTime")
    if exp is not None:
        try:
            res.exposure = f"1/{int(round(1/float(exp)))}" if float(exp) < 1 else str(exp)
        except (TypeError, ValueError, ZeroDivisionError):
            pass

    # GPS lives in a sub-IFD
    gps_ifd_id = tag_map.get("GPSInfo")
    if gps_ifd_id:
        gps = exif.get_ifd(gps_ifd_id) or {}
        if gps:
            gps_tags = {v: k for k, v in ExifTags.GPSTAGS.items()}
            res.latitude = _gps_to_float(
                gps.get(gps_tags.get("GPSLatitude")), gps.get(gps_tags.get("GPSLatitudeRef"))
            )
            res.longitude = _gps_to_float(
                gps.get(gps_tags.get("GPSLongitude")), gps.get(gps_tags.get("GPSLongitudeRef"))
            )
            alt = gps.get(gps_tags.get("GPSAltitude"))
            if alt is not None:
                try:
                    res.altitude = float(alt)
                except (TypeError, ValueError):
                    pass

    res.status = "ok" if res.taken_at else "partial"
    return res


_EXIFTOOL_TAGS = [
    "-DateTimeOriginal",
    "-CreateDate",
    "-ImageWidth",
    "-ImageHeight",
    "-Make",
    "-Model",
    "-LensModel",
    "-ISO",
    "-FNumber",
    "-FocalLength",
    "-ExposureTime",
    "-Orientation",
    "-GPSLatitude",
    "-GPSLongitude",
    "-GPSAltitude",
    "-Duration",
]


def _extract_exiftool(path: str) -> ExifResult:
    res = ExifResult(extractor="exiftool")
    # Goes through the per-thread persistent exiftool process (`-stay_open`).
    # Falls back to None if exiftool is not installed at all.
    data = exiftool_pool.fetch_metadata(path, _EXIFTOOL_TAGS)
    if data is None:
        res.status = "failed"
        res.error = "exiftool: not available or call failed"
        return res

    res.taken_at = _parse_dt(data.get("DateTimeOriginal")) or _parse_dt(data.get("CreateDate"))
    res.width = data.get("ImageWidth")
    res.height = data.get("ImageHeight")
    res.camera_make = (data.get("Make") or "").strip() or None
    res.camera_model = (data.get("Model") or "").strip() or None
    res.lens = (data.get("LensModel") or "").strip() or None
    res.iso = int(data["ISO"]) if data.get("ISO") is not None else None
    res.fnumber = float(data["FNumber"]) if data.get("FNumber") is not None else None
    res.focal_length = (
        float(data["FocalLength"]) if data.get("FocalLength") is not None else None
    )
    et = data.get("ExposureTime")
    if et is not None:
        try:
            etf = float(et)
            res.exposure = f"1/{int(round(1/etf))}" if etf < 1 else str(etf)
        except (TypeError, ValueError, ZeroDivisionError):
            pass
    res.orientation = int(data["Orientation"]) if data.get("Orientation") is not None else None
    res.latitude = data.get("GPSLatitude")
    res.longitude = data.get("GPSLongitude")
    res.altitude = data.get("GPSAltitude")
    res.duration_seconds = data.get("Duration")
    res.status = "ok" if res.taken_at else "partial"
    return res


_EXTRACTORS = {
    "pillow": _extract_pillow,
    "exiftool": _extract_exiftool,
}


def _has_required(r: ExifResult) -> bool:
    required = set(get_settings().exif.required_fields)
    for f in required:
        if getattr(r, f, None) is None:
            return False
    return True


_PILLOW_HOSTILE_EXTS = {
    "raw", "rw2", "arw", "cr2", "cr3", "nef", "orf", "pef", "dng",
    "raf", "srw", "rwl", "iiq",
    "heic", "heif", "avif",
}


def extract(path: str, *, media_kind: str) -> ExifResult:
    """Run the configured extractor chain. Returns the best-effort result.

    For videos and RAW formats, Pillow either can't help or is unreliable —
    we reorder the chain to try ExifTool first.
    """
    chain = list(get_settings().exif.extractor_chain)
    ext = path.lower().rsplit(".", 1)[-1] if "." in path else ""
    if media_kind == "video" or ext in _PILLOW_HOSTILE_EXTS:
        # Move pillow to the end (or drop it for videos entirely).
        chain = [c for c in chain if c != "pillow"]
        if "exiftool" not in chain:
            chain.insert(0, "exiftool")
        if media_kind != "video":
            chain.append("pillow")

    last: ExifResult | None = None
    for name in chain:
        fn = _EXTRACTORS.get(name)
        if fn is None:
            log.warning("unknown extractor in chain: %s", name)
            continue
        try:
            r = fn(path)
        except Exception as e:
            r = ExifResult(extractor=name, status="failed", error=f"{name}: {e}")
        if r.status == "ok" and _has_required(r):
            return r
        # Merge — keep previous values that this one didn't fill
        if last is not None:
            for slot, val in last.__dict__.items():
                if val is not None and getattr(r, slot, None) is None and slot not in (
                    "extractor",
                    "status",
                    "error",
                    "raw",
                ):
                    setattr(r, slot, val)
        last = r

    if last is None:
        return ExifResult(status="failed", error="no extractors ran")
    if _has_required(last):
        last.status = "ok"
    elif any(
        getattr(last, f) is not None
        for f in ("taken_at", "width", "camera_model", "latitude")
    ):
        last.status = "partial"
    else:
        last.status = "failed"
    return last

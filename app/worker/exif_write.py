"""ExifTool-based writes for the file's own EXIF (DateTimeOriginal,
GPSLatitude/Longitude, …). Used by the admin-only metadata-edit
endpoints to make the file the source of truth — so a re-index never
clobbers a user's edit and so the file remains self-describing if it's
copied to another system.

Mirrors `worker.rotate.rotate_orientation_tag` in structure:

  - take an already-resolved `tool` string (caller checks
    exiftool_path() availability so this module stays pure)
  - run with `-m` to ignore the [minor] complaints real-world EXIF
    blobs are full of (Nikon MakerNotes offsets, third-party tools'
    write quirks)
  - first try `-overwrite_original`, fall back to
    `-overwrite_original_in_place` if the temp-file path fails (some
    Synology share ACLs block companion-file creation but allow
    in-place writes)
  - return an `ExifWriteResult` the caller can hand straight back to
    HTTPException(500, ...)

Functions never touch the database — they take an absolute path and
return a result struct. Caller updates Photo / PhotoLocation rows and
the file's content_signature.
"""

from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass
from datetime import datetime

log = logging.getLogger(__name__)


@dataclass
class ExifWriteResult:
    ok: bool
    error: str = ""


def _run_exiftool(tool: str, tag_args: list[str], abs_path: str) -> ExifWriteResult:
    """Common subprocess invocation. See module docstring for the
    write-strategy fallback story."""
    def _run(extra_flag: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [tool, "-m", extra_flag, *tag_args, str(abs_path)],
            capture_output=True,
            timeout=30,
            check=False,
        )

    try:
        proc = _run("-overwrite_original")
        err = (proc.stderr or b"").decode("utf-8", errors="replace").strip()
        if proc.returncode != 0 and "_exiftool_tmp" in err:
            log.warning(
                "exif-write: %s tmp-file create failed, retrying "
                "with -overwrite_original_in_place", abs_path,
            )
            proc = _run("-overwrite_original_in_place")
            err = (proc.stderr or b"").decode("utf-8", errors="replace").strip()
    except (OSError, subprocess.SubprocessError) as e:
        log.exception("exif-write: exiftool launch failed for %s", abs_path)
        return ExifWriteResult(ok=False, error=f"exiftool launch: {e}")

    if proc.returncode != 0:
        log.warning(
            "exif-write: %s exiftool exit %d stderr=%r",
            abs_path, proc.returncode, err[:500],
        )
        return ExifWriteResult(
            ok=False, error=err[:200] or f"exiftool exit {proc.returncode}",
        )
    return ExifWriteResult(ok=True)


def _fmt_exif_dt(dt: datetime) -> str:
    """EXIF date strings use the ":" date separator + " " between date
    and time, e.g. "2024:10:12 13:45:30". Python's strftime gets the
    time part right but uses "-" for dates by default."""
    return dt.strftime("%Y:%m:%d %H:%M:%S")


def write_taken_at(tool: str, abs_path: str, dt: datetime | None) -> ExifWriteResult:
    """Set the capture-time EXIF tags on `abs_path` to `dt`, or clear
    them when `dt` is None.

    Writes DateTimeOriginal + CreateDate (and the SubSec variants iPhone
    uses) so cataloging tools that prefer one over the other stay in
    sync. ModifyDate is left alone — that's the "file last edited"
    timestamp, semantically different from the capture time.
    """
    val = _fmt_exif_dt(dt) if dt is not None else ""
    args = [
        f"-DateTimeOriginal={val}",
        f"-CreateDate={val}",
        # SubSec variants — iPhone HEIC and modern DSLRs write these
        # alongside the seconds-resolution form. Cataloging tools that
        # prefer the higher-resolution value would otherwise see the
        # old EXIF date through the SubSec path even after our edit.
        f"-SubSecDateTimeOriginal={val}",
        f"-SubSecCreateDate={val}",
    ]
    return _run_exiftool(tool, args, abs_path)


def write_gps(
    tool: str, abs_path: str,
    lat: float, lng: float, alt: float | None,
) -> ExifWriteResult:
    """Write GPS coordinates to `abs_path`.

    GPSLatitude/GPSLongitude take signed decimal degrees; we also write
    the Ref tags explicitly because the auto-derivation from sign isn't
    consistent across formats (HEIC sometimes drops it). GPSAltitude
    uses an unsigned magnitude + a 0/1 Ref where 0=above-sea-level,
    1=below, per the EXIF 2.32 spec.
    """
    args = [
        f"-GPSLatitude={lat}",
        f"-GPSLongitude={lng}",
        f"-GPSLatitudeRef={'N' if lat >= 0 else 'S'}",
        f"-GPSLongitudeRef={'E' if lng >= 0 else 'W'}",
    ]
    if alt is not None:
        args.append(f"-GPSAltitude={abs(alt)}")
        args.append(f"-GPSAltitudeRef={0 if alt >= 0 else 1}")
    return _run_exiftool(tool, args, abs_path)


def clear_gps(tool: str, abs_path: str) -> ExifWriteResult:
    """Remove every GPS-related EXIF tag from `abs_path`."""
    args = [
        "-GPSLatitude=",
        "-GPSLongitude=",
        "-GPSLatitudeRef=",
        "-GPSLongitudeRef=",
        "-GPSAltitude=",
        "-GPSAltitudeRef=",
    ]
    return _run_exiftool(tool, args, abs_path)

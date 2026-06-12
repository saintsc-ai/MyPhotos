"""Unit tests for app.worker.rotate.

The route handler now delegates to these helpers — keeping them
covered means a future refactor of bulk_rotate can't silently
break the rotation pipeline. ExifTool subprocess invocation is
NOT tested here (it needs a real binary + writable file); the
other helpers are pure-ish and worth pinning.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

import pytest
from PIL import Image

from app.worker import rotate as r


# ====================================================================
# next_orientation — pure data, exhaustive.
# ====================================================================


def test_next_orientation_cw_cycle():
    # Four CW rotations from 1 should return to 1.
    cur = 1
    for _ in range(4):
        cur = r.next_orientation(cur, "cw")
    assert cur == 1


def test_next_orientation_ccw_cycle():
    cur = 1
    for _ in range(4):
        cur = r.next_orientation(cur, "ccw")
    assert cur == 1


def test_next_orientation_180_is_self_inverse():
    for start in range(1, 9):
        once = r.next_orientation(start, "180")
        twice = r.next_orientation(once, "180")
        assert twice == start, f"180+180 from {start} should return {start}, got {twice}"


def test_next_orientation_cw_ccw_inverse():
    for start in range(1, 9):
        cw = r.next_orientation(start, "cw")
        back = r.next_orientation(cw, "ccw")
        assert back == start, f"cw then ccw from {start} should return, got {back}"


def test_next_orientation_handles_none_and_garbage():
    # Unknown / None / out-of-range current treated as 1 (upright default).
    assert r.next_orientation(None, "cw") == r.next_orientation(1, "cw")
    assert r.next_orientation(0, "cw") == r.next_orientation(1, "cw")
    assert r.next_orientation(99, "cw") == r.next_orientation(1, "cw")


# ====================================================================
# rehash_file — small fixture file, verify against hashlib directly.
# ====================================================================


def test_rehash_file_matches_hashlib(tmp_path: Path):
    payload = b"some bytes for the test " * 100
    p = tmp_path / "x.bin"
    p.write_bytes(payload)
    expected_sha = hashlib.sha256(payload).hexdigest()
    rh = r.rehash_file(str(p))
    assert rh.sha256 == expected_sha
    assert rh.file_size == len(payload)
    assert ":" in rh.content_signature
    size_s, mtime_s = rh.content_signature.split(":", 1)
    assert int(size_s) == len(payload)
    # mtime_ns matches what os.stat reports
    assert int(mtime_s) == os.stat(p).st_mtime_ns


def test_rehash_file_missing_raises(tmp_path: Path):
    with pytest.raises(OSError):
        r.rehash_file(str(tmp_path / "does-not-exist.bin"))


# ====================================================================
# regenerate_rotated_thumbnails — write a fake "old thumb" then check
# the rotated outputs land at the new sha path.
# ====================================================================


def test_regenerate_rotated_thumbnails_writes_at_new_sha(tmp_path: Path, monkeypatch):
    # Stand in a fake thumbnail directory; redirect _thumb_path there.
    fake_thumbs = tmp_path / "thumbs"
    fake_thumbs.mkdir()

    def fake_thumb_path(sha: str, size: int) -> Path:
        # sharded-by-prefix path mirroring the production layout
        return fake_thumbs / sha[:2] / sha / f"{size}.jpg"

    monkeypatch.setattr("app.worker.thumbs.thumb_path", fake_thumb_path)

    old_sha = "a" * 64
    new_sha = "b" * 64
    sizes = [256, 1024]

    # Seed a single old thumb at the largest size — helper picks it up
    # as the source for rotation.
    src_path = fake_thumb_path(old_sha, 1024)
    src_path.parent.mkdir(parents=True, exist_ok=True)
    img = Image.new("RGB", (400, 200), color=(120, 60, 30))
    img.save(src_path, format="JPEG", quality=90)

    result = r.regenerate_rotated_thumbnails(
        old_sha, new_sha, "cw", sizes, quality=88,
    )
    assert result.status == "ok"
    assert sorted(result.written_sizes) == sizes

    # Both target sizes exist at the NEW sha path.
    for size in sizes:
        out = fake_thumb_path(new_sha, size)
        assert out.exists(), f"missing thumb at size {size}"
        # CW rotation of a 400x200 image yields a portrait orientation;
        # after thumbnail(size,size) the longer side equals `size`.
        with Image.open(out) as im:
            w, h = im.size
            assert h > w, "expected portrait after CW rotation"


def test_regenerate_rotated_thumbnails_pending_when_no_prior(
    tmp_path: Path, monkeypatch,
):
    fake_thumbs = tmp_path / "thumbs"

    def fake_thumb_path(sha: str, size: int) -> Path:
        return fake_thumbs / sha[:2] / sha / f"{size}.jpg"

    monkeypatch.setattr("app.worker.thumbs.thumb_path", fake_thumb_path)

    # No file at the old sha → helper signals 'pending' (caller falls
    # back to the worker's full generate()).
    result = r.regenerate_rotated_thumbnails(
        "deadbeef" * 8, "feedface" * 8, "ccw", [256], quality=85,
    )
    assert result.status == "pending"
    assert result.written_sizes == []


# ====================================================================
# needs_pixel_rotation — the format classifier deciding tag-write vs
# in-place pixel re-encode.
# ====================================================================


def test_needs_pixel_rotation_classifies_formats():
    # Lossless raster with no usable EXIF orientation → pixel rotate.
    for ext in ("bmp", "png", "gif", "BMP", ".PNG", ".gif"):
        assert r.needs_pixel_rotation(ext), ext
    # Formats that carry a honoured EXIF Orientation tag → tag-write path.
    for ext in ("jpg", "jpeg", "heic", "tiff", "tif", "webp", "cr2", "nef"):
        assert not r.needs_pixel_rotation(ext), ext


# ====================================================================
# rotate_pixels_in_place — the BMP/PNG/GIF path. Re-encodes pixels back
# into the same format; this is what the "Writing of BMP files is not
# yet supported" ExifTool failure pushed us onto.
# ====================================================================


@pytest.mark.parametrize("ext,fmt", [("bmp", "BMP"), ("png", "PNG"), ("gif", "GIF")])
def test_rotate_pixels_in_place_cw_swaps_dimensions(tmp_path: Path, ext, fmt):
    src = tmp_path / f"img.{ext}"
    # Non-square so a 90° rotation must swap W/H. Distinct content keeps
    # the encoders from collapsing anything.
    Image.new("RGB", (200, 100), (200, 30, 30)).save(src)

    res = r.rotate_pixels_in_place(str(src), "cw")
    assert res.ok, res.error

    with Image.open(src) as im:
        assert im.format == fmt, im.format          # stayed in-format
        assert im.size == (100, 200), im.size       # dimensions swapped


def test_rotate_pixels_in_place_180_keeps_dimensions(tmp_path: Path):
    src = tmp_path / "img.bmp"
    Image.new("RGB", (200, 100), (10, 120, 200)).save(src)
    res = r.rotate_pixels_in_place(str(src), "180")
    assert res.ok, res.error
    with Image.open(src) as im:
        assert im.size == (200, 100)                # 180° preserves W/H


def test_rotate_pixels_in_place_preserves_gif_animation(tmp_path: Path):
    src = tmp_path / "anim.gif"
    # Three genuinely-different frames so the GIF optimiser can't dedupe.
    cols = [(255, 0, 0), (0, 255, 0), (0, 0, 255)]
    frames = [Image.new("RGB", (200, 100), c).convert("P") for c in cols]
    frames[0].save(
        src, save_all=True, append_images=frames[1:], duration=120, loop=0,
    )

    res = r.rotate_pixels_in_place(str(src), "cw")
    assert res.ok, res.error

    with Image.open(src) as im:
        assert im.format == "GIF"
        assert im.size == (100, 200)
        assert im.n_frames == 3                     # animation survived
        assert im.info.get("loop") == 0


def test_rotate_pixels_in_place_reports_failure_on_garbage(tmp_path: Path):
    bad = tmp_path / "not-an-image.bmp"
    bad.write_bytes(b"definitely not a bitmap")
    res = r.rotate_pixels_in_place(str(bad), "cw")
    assert not res.ok
    assert res.error  # short, user-facing reason

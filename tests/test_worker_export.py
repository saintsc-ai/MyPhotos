"""Unit tests for app.worker.export.

Covers the pure-IO bits the bulk-download route delegates to:
unique_arc_name, sweep_old_downloads, build_photo_zip. The route's
ACL + token handling is covered separately by the smoke suite.
"""

from __future__ import annotations

import os
import time
import zipfile
from pathlib import Path

from app.worker import export as e


# ====================================================================
# unique_arc_name
# ====================================================================


def test_unique_arc_name_first_use_returns_original():
    taken: set[str] = set()
    assert e.unique_arc_name(taken, "a.jpg") == "a.jpg"
    assert "a.jpg" in taken


def test_unique_arc_name_collision_appends_suffix():
    taken = {"a.jpg"}
    assert e.unique_arc_name(taken, "a.jpg") == "a_2.jpg"
    assert e.unique_arc_name(taken, "a.jpg") == "a_3.jpg"


def test_unique_arc_name_no_extension():
    taken = {"foo"}
    assert e.unique_arc_name(taken, "foo") == "foo_2"


def test_unique_arc_name_multi_dot_filename():
    taken = {"my.photo.JPG"}
    # Splits on the LAST dot only — "my.photo" + "_2" + ".JPG".
    assert e.unique_arc_name(taken, "my.photo.JPG") == "my.photo_2.JPG"


# ====================================================================
# sweep_old_downloads
# ====================================================================


def test_sweep_removes_only_old(tmp_path: Path):
    old = tmp_path / "download_old.zip"
    new = tmp_path / "download_new.zip"
    other = tmp_path / "not-a-download.txt"
    for p in (old, new, other):
        p.write_bytes(b"x")
    # Age the old one beyond the threshold.
    past = time.time() - 7200
    os.utime(old, (past, past))

    e.sweep_old_downloads(tmp_path, max_age_seconds=3600)

    assert not old.exists()
    assert new.exists()
    assert other.exists()   # the glob is download_*.zip — others untouched


def test_sweep_missing_dir_is_no_op(tmp_path: Path):
    # Should not raise even if the dir doesn't exist (best-effort).
    e.sweep_old_downloads(tmp_path / "does-not-exist")


# ====================================================================
# build_photo_zip
# ====================================================================


def test_build_zip_packs_files(tmp_path: Path):
    src1 = tmp_path / "a.jpg"
    src2 = tmp_path / "b.jpg"
    src1.write_bytes(b"AAA")
    src2.write_bytes(b"BBB")
    items = [
        e.ZipItem(photo_id=1, src_path=src1, suggested_name="a.jpg"),
        e.ZipItem(photo_id=2, src_path=src2, suggested_name="b.jpg"),
    ]
    dest = tmp_path / "out.zip"
    result = e.build_photo_zip(items, dest)
    assert result.added == 2
    assert result.skipped == []
    # Verify contents.
    with zipfile.ZipFile(dest, "r") as zf:
        names = sorted(zf.namelist())
        assert names == ["a.jpg", "b.jpg"]
        assert zf.read("a.jpg") == b"AAA"


def test_build_zip_dedupes_colliding_filenames(tmp_path: Path):
    src1 = tmp_path / "first/IMG.jpg"
    src2 = tmp_path / "second/IMG.jpg"
    for s in (src1, src2):
        s.parent.mkdir(parents=True, exist_ok=True)
        s.write_bytes(b"x")
    items = [
        e.ZipItem(photo_id=1, src_path=src1, suggested_name="IMG.jpg"),
        e.ZipItem(photo_id=2, src_path=src2, suggested_name="IMG.jpg"),
    ]
    dest = tmp_path / "out.zip"
    result = e.build_photo_zip(items, dest)
    assert result.added == 2
    with zipfile.ZipFile(dest, "r") as zf:
        assert sorted(zf.namelist()) == ["IMG.jpg", "IMG_2.jpg"]


def test_build_zip_skips_missing_files(tmp_path: Path):
    src1 = tmp_path / "exists.jpg"
    src1.write_bytes(b"x")
    items = [
        e.ZipItem(photo_id=1, src_path=src1, suggested_name="exists.jpg"),
        e.ZipItem(
            photo_id=42, src_path=tmp_path / "gone.jpg",
            suggested_name="gone.jpg",
        ),
    ]
    dest = tmp_path / "out.zip"
    result = e.build_photo_zip(items, dest)
    assert result.added == 1
    assert result.skipped == [42]
    with zipfile.ZipFile(dest, "r") as zf:
        assert zf.namelist() == ["exists.jpg"]


def test_build_zip_blank_suggested_name_falls_back_to_id(tmp_path: Path):
    src = tmp_path / "x"
    src.write_bytes(b"x")
    items = [
        e.ZipItem(photo_id=7, src_path=src, suggested_name=""),
    ]
    dest = tmp_path / "out.zip"
    result = e.build_photo_zip(items, dest)
    assert result.added == 1
    with zipfile.ZipFile(dest, "r") as zf:
        assert zf.namelist() == ["photo_7"]

"""Scanner utilities: path normalization, MIME detection, ignore filters.

Korean filenames in particular need NFC normalization — Windows/HFS+ use
NFD while ext4/btrfs commonly hold NFC, and SQLite compares byte-for-byte.
Without normalization, the same logical filename produces duplicate rows.
"""

from __future__ import annotations

import unicodedata
from pathlib import PurePosixPath
from typing import Iterable

from ..config import get_settings


def nfc(s: str) -> str:
    """Normalize to NFC (composed form). Stable across filesystems."""
    return unicodedata.normalize("NFC", s)


def to_posix_rel(abs_path: str, root_abs: str) -> str:
    """Convert an absolute filesystem path to a POSIX-style relative path
    under `root_abs`. Always forward slashes, NFC-normalized, no leading slash.
    """
    abs_n = nfc(abs_path).replace("\\", "/")
    root_n = nfc(root_abs).replace("\\", "/").rstrip("/")
    if not abs_n.startswith(root_n + "/"):
        raise ValueError(f"{abs_path!r} is not under root {root_abs!r}")
    return abs_n[len(root_n) + 1 :]


def join_root(root_abs: str, rel: str) -> str:
    """Inverse of to_posix_rel. Returns OS-native path string (no Path object
    so the caller can hand it straight to os.* / open())."""
    return str(PurePosixPath(root_abs) / rel)


def classify(filename: str) -> tuple[str | None, str | None]:
    """Return (media_kind, ext) or (None, None) if the file is not indexable.

    media_kind ∈ {'image', 'video'}, ext is lowercase without dot.
    """
    s = get_settings()
    name = filename.lower()
    if "." not in name:
        return (None, None)
    ext = name.rsplit(".", 1)[1]
    if ext in s.scanner.image_extensions:
        return ("image", ext)
    if ext in s.scanner.video_extensions:
        return ("video", ext)
    return (None, None)


def is_ignored_dir(name: str) -> bool:
    return name in set(get_settings().scanner.ignore_dirs)


def is_ignored_file(name: str) -> bool:
    s = get_settings()
    if name in set(s.scanner.ignore_files):
        return True
    # Hidden files (dotfiles) are skipped on POSIX.
    if name.startswith("."):
        return True
    return False


def filter_dir_entries(entries: Iterable["os.DirEntry"]) -> tuple[list, list]:
    """Split DirEntry list into (subdirs, files) honoring ignore rules.

    Only files that classify() recognizes as image/video are returned —
    the caller doesn't need to re-check.
    """
    subdirs: list = []
    files: list = []
    for e in entries:
        name = nfc(e.name)
        try:
            if e.is_dir(follow_symlinks=False):
                if not is_ignored_dir(name):
                    subdirs.append(e)
            elif e.is_file(follow_symlinks=False):
                if is_ignored_file(name):
                    continue
                kind, _ = classify(name)
                if kind is not None:
                    files.append(e)
        except OSError:
            # Permission denied, broken symlink, etc. Skip silently.
            continue
    return subdirs, files

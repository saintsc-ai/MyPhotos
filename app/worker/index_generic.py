"""Handler for the 'index_file_generic' job — the light index pass for a
File row under a ``kind='file'`` root.

The scanner (discover_files_root) already set name / ext / size / mtime /
mime-by-extension inline, so the file is browsable and name-searchable the
moment it's discovered. This job fills in the parts that need to read the
file's bytes:

  1. sha256 (integrity + future dedup),
  2. (later phase) content-text extraction → file_fts, tracked by
     File.text_status.

Kept deliberately tiny and media-free — no EXIF, thumbnails, or ML. Content
extraction is a separate phase; until it ships, text_status stays 'pending'.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from sqlalchemy.orm import Session

from ..models import File, Root
from ..scanner.utils import join_root
from .index_file import _sha256_file

log = logging.getLogger(__name__)


def run(db: Session, payload: dict[str, Any]) -> None:
    file_id = int(payload["file_id"])
    f = db.get(File, file_id)
    if f is None:
        log.warning("index_file_generic: file %d not found, skipping", file_id)
        return
    # Trashed files live elsewhere / shouldn't be re-touched.
    if f.status == "trashed":
        return
    root = db.get(Root, f.root_id)
    if root is None:
        log.warning("index_file_generic: root %d not found for file %d",
                    f.root_id, file_id)
        return

    abs_path = join_root(root.abs_path, f.rel_path)
    if not os.path.exists(abs_path):
        f.status = "missing"
        db.commit()
        log.info("index_file_generic: %s no longer exists, marked missing", abs_path)
        return

    if not f.sha256:
        try:
            f.sha256 = _sha256_file(abs_path)
        except OSError as e:
            log.warning("index_file_generic: hash failed for %s: %s", abs_path, e)
            return
        db.commit()

    # Content-text extraction (PDF/office/HWP/OCR) is a later phase; the
    # extractor registry will pick up rows with text_status='pending'.

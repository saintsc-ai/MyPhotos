"""SQLite FTS5 virtual-table sync + query helpers for unified search.

Why this module exists:

The old `text_q` unified search was an OR of LIKE expressions across
filename + rel_path + description + comment body + tag name + uploader
username. Each LIKE %needle% forces a full table scan; at ~100 k photos
the gallery's search box is visibly sluggish, and the indexed-text
subqueries fan out to the join tables every hit.

This module exposes a single FTS5 virtual table (`photo_fts`) whose
rowid = `photos.id` and whose only column is `text` — a
space-joined bag of every searchable string for that photo. The
caller side stays simple: build the bag in Python on any mutation,
DELETE+INSERT into the FTS table. The migration creates the table
with `tokenize='trigram'` so Korean substring matches ("고양이"
inside "고양이바다") work the same way English substring matches do
("cation" inside "vacation"). Tokens shorter than 3 chars still fall
back to LIKE on the caller side; `build_match_query` returns None
for those so the route can branch.

Note: the FTS5 table is intentionally NOT contentless — contentless
tables reject DELETE, but the rebuild path is DELETE+INSERT per id.
The ~100 MB of extra disk at 100k photos is negligible on a NAS.

All write paths must call `rebuild_photo(db, photo_id)` after their
commit (or `delete_photo` on permanent purge). If the migration
hasn't run yet, every helper here no-ops — `is_available` is the
feature gate so we don't have to ship two code paths.
"""

from __future__ import annotations

from typing import Iterable, Optional

from sqlalchemy import bindparam, text
from sqlalchemy.orm import Session


FTS_TABLE = "photo_fts"


# Single composed-text SELECT — joined as a literal so callers can use
# it both for per-photo rebuild (WHERE p.id = :pid) and migration
# backfill (no WHERE, all rows). GROUP_CONCAT uses a space separator
# so trigrams don't accidentally span across two unrelated tokens.
_COMPOSE_BODY = """
    COALESCE(p.filename, '')
    || ' ' || COALESCE(p.rel_path, '')
    || ' ' || COALESCE(p.description, '')
    || ' ' || COALESCE(
         (SELECT GROUP_CONCAT(c.body, ' ')
            FROM photo_comments c WHERE c.photo_id = p.id), '')
    || ' ' || COALESCE(
         (SELECT GROUP_CONCAT(t.name, ' ')
            FROM photo_tags pt JOIN tags t ON pt.tag_id = t.id
            WHERE pt.photo_id = p.id), '')
    || ' ' || COALESCE(
         (SELECT GROUP_CONCAT(t.name, ' ')
            FROM photo_auto_tags pat JOIN tags t ON pat.tag_id = t.id
            WHERE pat.photo_id = p.id), '')
    || ' ' || COALESCE(
         (SELECT u.username FROM users u WHERE u.id = p.owner_user_id), '')
    || ' ' || COALESCE(p.ocr_text, '')
    || ' ' || COALESCE(p.camera_make, '')
    || ' ' || COALESCE(p.camera_model, '')
    || ' ' || COALESCE(p.lens, '')
    -- Person names — every distinct face cluster label on this photo.
    -- DISTINCT so a group shot with three faces of one person doesn't
    -- triple-pad the bag; nulls (unnamed clusters) drop out via the
    -- WHERE. Lets the unified search box match "엄마" against any
    -- photo whose face was assigned to that cluster.
    || ' ' || COALESCE(
         (SELECT GROUP_CONCAT(DISTINCT fc.label)
            FROM photo_faces pf JOIN face_clusters fc ON pf.cluster_id = fc.id
            WHERE pf.photo_id = p.id AND fc.label IS NOT NULL), '')
    -- Object labels (YOLO detections + user-drawn). Same DISTINCT
    -- treatment so three dogs don't appear as "dog dog dog".
    || ' ' || COALESCE(
         (SELECT GROUP_CONCAT(DISTINCT po.label)
            FROM photo_objects po
            WHERE po.photo_id = p.id), '')
"""


_availability_cache: Optional[bool] = None


def is_available(db: Session) -> bool:
    """Feature-detect the FTS table — true once alembic 0020 has run.

    FTS5 is a SQLite-only feature; the migration that creates
    `photo_fts` is gated to SQLite, and the trigram tokenizer +
    contentless table syntax don't exist on MariaDB / PostgreSQL.
    So short-circuit to False on non-SQLite backends — search code
    that calls into this module then takes the "FTS not available"
    branch (currently returns no rows; a LIKE-OR fallback is the
    documented next step). Otherwise feature-detect via sqlite_master.

    Cached in module-global so we don't query on every request.
    Restart the process after running new migrations (we already do
    that on deploy).
    """
    global _availability_cache
    if _availability_cache is not None:
        return _availability_cache
    if db.bind.dialect.name != "sqlite":
        _availability_cache = False
        return _availability_cache
    row = db.execute(
        text(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=:n"
        ),
        {"n": FTS_TABLE},
    ).first()
    _availability_cache = row is not None
    return _availability_cache


def _reset_availability_cache() -> None:
    """Test hook — flush the feature-detect cache so a freshly-
    migrated in-memory DB is detected without a process restart."""
    global _availability_cache
    _availability_cache = None


def rebuild_photo(db: Session, photo_id: int) -> None:
    """Recompute and re-insert the FTS row for one photo.

    Contentless FTS5 doesn't support UPDATE — DELETE + INSERT is the
    canonical idiom. Wrapped in a no-op when the table isn't there
    yet so call sites in routes/workers don't have to feature-detect.

    Does NOT commit — the caller's existing commit (for whatever
    write triggered this) flushes the FTS write in the same
    transaction.
    """
    if not is_available(db):
        return
    db.execute(
        text(f"DELETE FROM {FTS_TABLE} WHERE rowid = :pid"),
        {"pid": int(photo_id)},
    )
    db.execute(
        text(
            f"INSERT INTO {FTS_TABLE}(rowid, text) "
            f"SELECT p.id, {_COMPOSE_BODY} FROM photos p WHERE p.id = :pid"
        ),
        {"pid": int(photo_id)},
    )


def bulk_rebuild(db: Session, photo_ids: Iterable[int]) -> None:
    """Rebuild many photos at once — used by bulk-tag / bulk-delete
    style endpoints so we don't fire one DELETE+INSERT pair per id.

    Chunks at 900 placeholders per statement. SQLite caps parameters
    per statement at SQLITE_LIMIT_VARIABLE_NUMBER — 32766 since 3.32,
    999 in older builds. A popular face cluster ("성민아": 70k+
    photos) blows even the new ceiling in one go; 900 is comfortably
    under both. The chunks share a transaction with the caller, so
    a partial failure rolls the whole rebuild back the same way a
    single big statement would.
    """
    if not is_available(db):
        return
    ids = [int(p) for p in photo_ids]
    if not ids:
        return
    CHUNK = 900
    for off in range(0, len(ids), CHUNK):
        chunk = ids[off:off + CHUNK]
        # bindparam(expanding) on raw text — SQLAlchemy expands `:ids`
        # to (?, ?, ?, ...) at execute time when the param is a list.
        db.execute(
            text(f"DELETE FROM {FTS_TABLE} WHERE rowid IN :ids").bindparams(
                bindparam("ids", expanding=True)
            ),
            {"ids": chunk},
        )
        db.execute(
            text(
                f"INSERT INTO {FTS_TABLE}(rowid, text) "
                f"SELECT p.id, {_COMPOSE_BODY} FROM photos p WHERE p.id IN :ids"
            ).bindparams(bindparam("ids", expanding=True)),
            {"ids": chunk},
        )


def delete_photo(db: Session, photo_id: int) -> None:
    """Drop the FTS row for a permanently purged photo. Soft-deletes
    (trash) keep their FTS row so restored photos stay searchable."""
    if not is_available(db):
        return
    db.execute(
        text(f"DELETE FROM {FTS_TABLE} WHERE rowid = :pid"),
        {"pid": int(photo_id)},
    )


# ---------------------------------------------------------------------------
# files domain (kind='file' roots) — separate FTS5 table, same trigram idiom
# as photo_fts. Phase 2 indexes filename + rel_path (searchable immediately);
# extracted document *content* is folded in by a later phase (Phase 3).
# ---------------------------------------------------------------------------
FILE_FTS_TABLE = "file_fts"

_FILE_COMPOSE_BODY = """
    COALESCE(f.filename, '')
    || ' ' || COALESCE(f.rel_path, '')
    || ' ' || COALESCE(ft.body, '')
"""
# JOIN clause pairing files f with its extracted-text row (may be absent).
_FILE_FROM = "files f LEFT JOIN file_text ft ON ft.file_id = f.id"

_file_availability_cache: Optional[bool] = None


def is_file_fts_available(db: Session) -> bool:
    """Feature-detect `file_fts` (created by alembic 0038, SQLite only)."""
    global _file_availability_cache
    if _file_availability_cache is not None:
        return _file_availability_cache
    if db.bind.dialect.name != "sqlite":
        _file_availability_cache = False
        return _file_availability_cache
    row = db.execute(
        text("SELECT 1 FROM sqlite_master WHERE type='table' AND name=:n"),
        {"n": FILE_FTS_TABLE},
    ).first()
    _file_availability_cache = row is not None
    return _file_availability_cache


def rebuild_file(db: Session, file_id: int) -> None:
    """Recompute the FTS row for one file. DELETE+INSERT (no UPDATE on FTS5).
    Does not commit — shares the caller's transaction."""
    if not is_file_fts_available(db):
        return
    db.execute(
        text(f"DELETE FROM {FILE_FTS_TABLE} WHERE rowid = :fid"),
        {"fid": int(file_id)},
    )
    db.execute(
        text(
            f"INSERT INTO {FILE_FTS_TABLE}(rowid, text) "
            f"SELECT f.id, {_FILE_COMPOSE_BODY} FROM {_FILE_FROM} WHERE f.id = :fid"
        ),
        {"fid": int(file_id)},
    )


def bulk_rebuild_files(db: Session, file_ids: Iterable[int]) -> None:
    """Rebuild many file FTS rows at once (900-id chunks, as bulk_rebuild)."""
    if not is_file_fts_available(db):
        return
    ids = [int(f) for f in file_ids]
    if not ids:
        return
    CHUNK = 900
    for off in range(0, len(ids), CHUNK):
        chunk = ids[off:off + CHUNK]
        db.execute(
            text(f"DELETE FROM {FILE_FTS_TABLE} WHERE rowid IN :ids").bindparams(
                bindparam("ids", expanding=True)
            ),
            {"ids": chunk},
        )
        db.execute(
            text(
                f"INSERT INTO {FILE_FTS_TABLE}(rowid, text) "
                f"SELECT f.id, {_FILE_COMPOSE_BODY} FROM {_FILE_FROM} WHERE f.id IN :ids"
            ).bindparams(bindparam("ids", expanding=True)),
            {"ids": chunk},
        )


def delete_file(db: Session, file_id: int) -> None:
    """Drop the FTS row for a permanently purged file."""
    if not is_file_fts_available(db):
        return
    db.execute(
        text(f"DELETE FROM {FILE_FTS_TABLE} WHERE rowid = :fid"),
        {"fid": int(file_id)},
    )


def reindex_all(db: Session, *, batch: int = 2000) -> int:
    """Rebuild every photo's FTS row in committed batches. Run after
    changing _COMPOSE_BODY (e.g. adding camera fields) so existing rows
    pick up the new text. Batched + per-batch commit keeps the SQLite
    writer lock short (won't starve the OCR/ML workers). Returns the
    number of photos reindexed."""
    if not is_available(db):
        return 0
    ids = [r[0] for r in db.execute(text("SELECT id FROM photos")).all()]
    done = 0
    for off in range(0, len(ids), batch):
        chunk = ids[off:off + batch]
        bulk_rebuild(db, chunk)
        db.commit()
        done += len(chunk)
    return done


def build_match_query(needle: str) -> Optional[str]:
    """Turn a user-typed search string into an FTS5 MATCH expression,
    or return None when the input can't safely become one.

    Trigram tokenizer needs at least 3 characters of usable input. For
    1–2 char queries the caller should fall back to LIKE so the user
    isn't told "no results" for a query that's not actually empty.

    Quoted-phrase form (`"..."`) keeps user-supplied punctuation,
    spaces, and special FTS5 operators (`*`, `:`, `-`, `^`, `(`)
    out of the parser. Double-quotes inside the needle are doubled
    per FTS5's quoting rules.
    """
    if not needle:
        return None
    s = needle.strip()
    # FTS5 trigram needs 3+ chars total; spaces aren't counted as
    # token material but we don't try to deduce per-token length
    # ahead of MATCH — just gate the whole string.
    if len(s) < 3:
        return None
    escaped = s.replace('"', '""')
    return f'"{escaped}"'


def matching_photo_ids_sql() -> str:
    """Raw SQL fragment returning matching photo ids — meant to be
    nested under `WHERE photos.id IN (...)` in routes. Caller supplies
    the `:match` bind for the MATCH expression built by
    `build_match_query`. Pulled out as a helper so the SQL keeps the
    same shape across multiple endpoints (list, tags, histogram, …).
    """
    return f"SELECT rowid FROM {FTS_TABLE} WHERE {FTS_TABLE} MATCH :match"

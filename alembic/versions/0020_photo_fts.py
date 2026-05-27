"""Add photo_fts virtual table (FTS5, trigram tokenizer) for unified search

Revision ID: 0020_photo_fts
Revises: 0019_share_strip_exif
Create Date: 2026-05-28 12:00:00

Replaces the OR-of-LIKE substring scan in `text_q` with a single
FTS5 MATCH lookup over a composed bag-of-text per photo (filename +
rel_path + description + comment bodies + tag names + uploader
username). Trigram tokenizer chosen so Korean substring queries
("양이바" inside "고양이바다") work the same way English substring
queries do ("cation" inside "vacation").

Initial backfill runs as a single INSERT … SELECT so a 100k-photo
catalog populates in one query. Subsequent maintenance happens via
`app.fts.rebuild_photo` calls at every write path.

Trigram tokenizer requires SQLite 3.34+ (2021). We refuse to run on
older builds rather than silently falling back to unicode61 — that
would index the data incompatibly and produce wrong results for
Korean substring queries until a backfill re-runs.
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op

revision: str = "0020_photo_fts"
down_revision: Union[str, None] = "0019_share_strip_exif"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# Same expression as app.fts._COMPOSE_BODY — kept in sync by hand
# (don't import from app code inside an alembic revision, the
# downgrade path must work standalone).
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
"""


def upgrade() -> None:
    bind = op.get_bind()
    # Trigram is only available on SQLite 3.34+. Probe with a temp
    # table — if the tokenize spec is rejected, abort with an
    # actionable message rather than creating a degraded index.
    try:
        bind.exec_driver_sql(
            "CREATE VIRTUAL TABLE _fts_probe USING fts5(x, tokenize='trigram')"
        )
        bind.exec_driver_sql("DROP TABLE _fts_probe")
    except Exception as exc:
        raise RuntimeError(
            "SQLite build lacks FTS5 trigram tokenizer (need 3.34+). "
            "Upgrade Python's sqlite3 module or skip this migration."
        ) from exc

    # Default content mode (NOT contentless): contentless tables
    # reject DELETE outright, but our rebuild_photo path is
    # DELETE+INSERT per id. The extra disk cost for storing the bag
    # twice is ~100 MB at 100k photos — fine on a NAS, much simpler
    # than tracking previous text for the contentless "delete"
    # command. rowid = photos.id so cross-joins are still trivial.
    bind.exec_driver_sql(
        "CREATE VIRTUAL TABLE photo_fts USING fts5("
        "  text, tokenize='trigram'"
        ")"
    )

    # Backfill every existing photo in one shot. SQLite handles this
    # as a single statement so even 100k photos finish in a few
    # seconds on the NAS. New photos added after the migration are
    # indexed by rebuild_photo() at the end of the scanner pipeline.
    bind.exec_driver_sql(
        "INSERT INTO photo_fts(rowid, text) "
        f"SELECT p.id, {_COMPOSE_BODY} FROM photos p"
    )


def downgrade() -> None:
    op.get_bind().exec_driver_sql("DROP TABLE IF EXISTS photo_fts")

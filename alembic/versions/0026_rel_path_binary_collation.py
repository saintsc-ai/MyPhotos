"""Switch rel_path columns to utf8mb4_bin on MariaDB for case sensitivity.

Revision ID: 0026_rel_path_binary_collation
Revises: 0025_photos_file_size_bigint
Create Date: 2026-06-01

SQLite → MariaDB migration died at row 785 of photos with:

    pymysql.err.IntegrityError (1062, "Duplicate entry
    '1-ysseo_iphone/2025/04/IMG_6528.mov' for key
    'uq_photos_root_relpath'")

SQLite's default text comparison is BINARY (byte-for-byte). MariaDB's
default collation `utf8mb4_unicode_ci` is CASE-INSENSITIVE and also
folds NFC/NFD Unicode normalization forms. So pairs that SQLite stored
as distinct rows ('IMG_6528.mov' and 'IMG_6528.MOV', or NFC vs NFD
Korean paths from a Mac vs Linux source) collide on the
UNIQUE(root_id, rel_path) index during INSERT on MariaDB.

Fix: pin both rel_path columns to `utf8mb4_bin` so MariaDB compares
byte-for-byte, matching SQLite. No change for SQLite (collation
silently ignored — already binary) or PostgreSQL (default text
comparison is already case-sensitive).

This complements 0024 (TEXT → VARCHAR(512) for InnoDB key length).
0024 fixed CREATE TABLE; 0026 fixes the runtime collision on INSERT.

  - On MariaDB: ALTER both columns with explicit CHARACTER SET +
    COLLATE. Existing rows are re-encoded (no-op when already utf8mb4)
    and the unique index gets rebuilt under the new collation. If the
    table already has case-only duplicates the ALTER itself will fail
    with 1062 — clean those out first with the dedup query in
    docs/operations/external-db.md.
  - On SQLite / PostgreSQL: no-op.
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op

revision: str = "0026_rel_path_binary_collation"
down_revision: Union[str, None] = "0025_photos_file_size_bigint"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "mysql":
        # SQLite collation is BINARY by default; PostgreSQL text
        # comparison is already case-sensitive. Only MariaDB / MySQL
        # need the explicit binary collation override.
        return
    for table in ("photos", "uploads_pending"):
        op.execute(
            f"ALTER TABLE {table} "
            f"MODIFY rel_path VARCHAR(512) "
            f"CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL"
        )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "mysql":
        return
    # Revert to the table's default (typically utf8mb4_unicode_ci). If
    # any rows have case-only duplicates this will collide on the
    # unique index — that's the symptom we're protecting against, so
    # it's correct for the downgrade to surface it.
    for table in ("photos", "uploads_pending"):
        op.execute(
            f"ALTER TABLE {table} "
            f"MODIFY rel_path VARCHAR(512) NOT NULL"
        )

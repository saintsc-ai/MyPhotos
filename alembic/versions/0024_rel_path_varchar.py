"""Change photos.rel_path + uploads_pending.rel_path from TEXT to VARCHAR(512).

Revision ID: 0024_rel_path_varchar
Revises: 0023_folder_acl_path_prefix_varchar
Create Date: 2026-06-01

Same MariaDB constraint as 0023: TEXT/BLOB columns can't appear in a
key specification without an explicit prefix length —

    ERROR 1170 "BLOB/TEXT column 'rel_path' used in key specification
    without a key length"

Both photos and uploads_pending have UNIQUE(root_id, rel_path), so a
fresh MariaDB target (scripts/migrate-db.py with --drop, or
alembic upgrade head on an empty MariaDB DB) fails at CREATE TABLE
photos. SQLite happily accepts the TEXT-in-key, which is why this
went undetected until external-DB migration testing.

Fix: declare rel_path as VARCHAR(512). 512 chars × 4 bytes (utf8mb4) =
2048 bytes; with the INT root_id sibling the composite key fits well
under InnoDB's 3072-byte limit. 512 chars is far above any realistic
photo path (Synology / Windows / Linux all in the 200-300 char range
for deeply nested catalogs).

  - On MariaDB: ALTERs the columns. No data loss in practice — the
    longest rel_path in real catalogs has never approached 512.
  - On SQLite: no-op (VARCHAR length is advisory; SQLite stores TEXT
    either way). Revision still gets stamped so MariaDB targets pick
    up the new schema cleanly.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0024_rel_path_varchar"
down_revision: Union[str, None] = "0023_folder_acl_path_prefix_varchar"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        # SQLite doesn't enforce VARCHAR length and ALTER COLUMN with
        # batch_alter_table would rebuild the entire table just to
        # change a type SQLite ignores. Cheaper to skip.
        return
    for table in ("photos", "uploads_pending"):
        with op.batch_alter_table(table) as batch:
            batch.alter_column(
                "rel_path",
                existing_type=sa.Text(),
                type_=sa.String(length=512),
                existing_nullable=False,
            )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        return
    for table in ("photos", "uploads_pending"):
        with op.batch_alter_table(table) as batch:
            batch.alter_column(
                "rel_path",
                existing_type=sa.String(length=512),
                type_=sa.Text(),
                existing_nullable=False,
            )

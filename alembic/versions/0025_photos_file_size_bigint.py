"""Widen photos.file_size from INT to BIGINT for MariaDB / PostgreSQL.

Revision ID: 0025_photos_file_size_bigint
Revises: 0024_rel_path_varchar
Create Date: 2026-06-01

Migration from SQLite → MariaDB stopped partway through the photos
copy with:

    pymysql.err.DataError (1264, "Out of range value for column
    'file_size' at row 785")

SQLAlchemy's Integer maps to signed INT(11) on MySQL / MariaDB and
INTEGER on PostgreSQL — both 32-bit, so the max value is
2,147,483,647 (≈ 2 GB). A single multi-GB iPhone 4K video easily
exceeds that. SQLite stores all INTEGER as 64-bit regardless, which
is why the original column never overflowed under the bundled
backend.

Switched to BigInteger so the column lands as BIGINT(20) (MariaDB) /
BIGINT (PostgreSQL) — signed 64-bit, max ≈ 9.2 EB. SQLite is
unaffected (already 64-bit).

  - On MariaDB / PostgreSQL: ALTER the column. The index on file_size
    is preserved by batch_alter_table.
  - On SQLite: no-op (BigInteger and Integer are both 64-bit there).

Other Integer columns audited: width / height (pixels), iso,
orientation, scan_interval, view_count, progress_done — all stay
well under 2B for any realistic catalog. Only file_size has a
practical overflow path, so this migration is scoped to one column.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0025_photos_file_size_bigint"
down_revision: Union[str, None] = "0024_rel_path_varchar"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        # SQLite stores all INTEGER as 64-bit — the original column
        # already accommodates BIGINT values. Skip the table rebuild.
        return
    with op.batch_alter_table("photos") as batch:
        batch.alter_column(
            "file_size",
            existing_type=sa.Integer(),
            type_=sa.BigInteger(),
            existing_nullable=True,
        )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        return
    with op.batch_alter_table("photos") as batch:
        batch.alter_column(
            "file_size",
            existing_type=sa.BigInteger(),
            type_=sa.Integer(),
            existing_nullable=True,
        )

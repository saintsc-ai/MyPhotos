"""Change folder_acl.path_prefix from TEXT to VARCHAR(512) for MariaDB.

Revision ID: 0023_folder_acl_path_prefix_varchar
Revises: 0022_job_progress
Create Date: 2026-06-01

MariaDB / MySQL refuses TEXT or BLOB columns inside a PRIMARY KEY
without an explicit prefix length:

    ERROR 1170 "BLOB/TEXT column 'path_prefix' used in key
    specification without a key length"

So Base.metadata.create_all() on a fresh MariaDB target (e.g. via
scripts/migrate-db.py) fails at this table. SQLite happily makes the
PK index on TEXT, so we never noticed before MariaDB testing started.

Fix: declare path_prefix as VARCHAR(512) instead. 512 chars × 4 bytes
(utf8mb4) = 2048 bytes; leaves headroom under InnoDB's 3072-byte
composite-PK ceiling for the two INT siblings (root_id, user_id) and
per-row overhead. 512 chars is still far above any realistic folder
path.

  - On MariaDB: ALTERs the column type. Reads what's there and
    re-creates the table with the new type (path_prefix has no rows
    > 768 chars in practice; if it ever does the migration fails
    visibly).
  - On SQLite: VARCHAR length is purely advisory (SQLite stores TEXT
    either way), so this migration is a no-op. We still bump the
    revision so MariaDB targets get the new schema cleanly.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0023_folder_acl_path_prefix_varchar"
down_revision: Union[str, None] = "0022_job_progress"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        # SQLite doesn't enforce VARCHAR length and ALTER COLUMN with
        # batch_alter_table would rebuild the entire table just to
        # change a type SQLite ignores. Cheaper to skip.
        return
    # MySQL / MariaDB: real ALTER. existing_type is what was declared
    # in 0014_folder_acl.py.
    with op.batch_alter_table("folder_acl") as batch:
        batch.alter_column(
            "path_prefix",
            existing_type=sa.Text(),
            type_=sa.String(length=512),
            existing_nullable=False,
        )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        return
    with op.batch_alter_table("folder_acl") as batch:
        batch.alter_column(
            "path_prefix",
            existing_type=sa.String(length=512),
            type_=sa.Text(),
            existing_nullable=False,
        )

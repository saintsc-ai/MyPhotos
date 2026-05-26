"""Add roots.ignore_paths for per-root indexing exclusions

Revision ID: 0011_root_ignore_paths
Revises: 0010_photo_file_size_index
Create Date: 2026-05-26 22:00:00

User-managed list of relative paths under each root that the scanner
should skip and that the gallery / search / stats should treat as
non-existent. Stored as JSON text — small enough that a separate
table would be overkill, and there are never more than a handful of
ignore entries per root.

Photos that land under an ignore path get status='ignored' (alongside
the existing active / trashed / missing values) so their ratings,
tags, comments survive an ignore toggle without being lost.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0011_root_ignore_paths"
down_revision: Union[str, None] = "0010_photo_file_size_index"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("roots") as batch:
        batch.add_column(sa.Column("ignore_paths", sa.Text(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("roots") as batch:
        batch.drop_column("ignore_paths")

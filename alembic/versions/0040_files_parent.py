"""files.parent — denormalised containing folder for fast explorer listing

Revision ID: 0040_files_parent
Revises: 0039_file_text
Create Date: 2026-07-02

Adds files.parent (the POSIX folder path, '' for root-level) + an index on
(root_id, parent) so the file explorer lists a folder by indexed equality
and derives subfolders with an index-only DISTINCT — instead of the previous
leading-wildcard LIKE scan over the whole subtree (10s+ on large roots).
Backfills parent from rel_path.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0040_files_parent"
down_revision: Union[str, None] = "0039_file_text"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "files",
        sa.Column("parent", sa.String(length=512), nullable=False,
                  server_default=""),
    )
    op.create_index("ix_files_root_parent", "files", ["root_id", "parent"])
    # Backfill parent = rel_path up to the last '/'. Done in Python so the
    # path split is identical to the app's (rsplit on '/'); one-time cost.
    bind = op.get_bind()
    rows = bind.exec_driver_sql("SELECT id, rel_path FROM files").fetchall()
    for fid, rel in rows:
        parent = rel.rsplit("/", 1)[0] if rel and "/" in rel else ""
        bind.exec_driver_sql(
            "UPDATE files SET parent = ? WHERE id = ?"
            if bind.dialect.name == "sqlite"
            else "UPDATE files SET parent = %s WHERE id = %s",
            (parent, fid),
        )


def downgrade() -> None:
    op.drop_index("ix_files_root_parent", table_name="files")
    op.drop_column("files", "parent")

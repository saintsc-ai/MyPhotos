"""Files (document) domain — roots.kind + files + file_fts + share_file_items

Revision ID: 0038_files_domain
Revises: 0037_photo_work_stage_params
Create Date: 2026-07-02

Adds lightweight general-file management alongside photos (see
docs/FILES_PLAN.md). A root now carries a `kind` ('photo' | 'file'); the
scanner branches on it so photo and document folders stay separate. Files
live in their own `files` table (no media pipeline), get shared via
`share_file_items` (photo shares in `share_items` are untouched), and are
content-searched through `file_fts` — a FTS5 trigram table mirroring
`photo_fts`, created only on SQLite (same feature-gate as 0020).
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0038_files_domain"
down_revision: Union[str, None] = "0037_photo_work_stage_params"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1) roots.kind — default 'photo' so every existing root keeps its
    #    current (media) behaviour with no data change.
    op.add_column(
        "roots",
        sa.Column(
            "kind", sa.String(length=16), nullable=False,
            server_default=sa.text("'photo'"),
        ),
    )

    # 2) files — sibling to photos, no media columns.
    op.create_table(
        "files",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("root_id", sa.Integer(), nullable=False),
        sa.Column("rel_path", sa.String(length=512), nullable=False),
        sa.Column("filename", sa.String(length=512), nullable=False),
        sa.Column("ext", sa.String(length=32), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=True),
        sa.Column("mime", sa.String(length=128), nullable=True),
        sa.Column("file_size", sa.BigInteger(), nullable=True),
        sa.Column("mtime", sa.DateTime(), nullable=True),
        sa.Column("content_signature", sa.String(length=64), nullable=True),
        sa.Column(
            "status", sa.String(length=16), nullable=False,
            server_default=sa.text("'active'"),
        ),
        sa.Column("text_status", sa.String(length=16), nullable=True),
        sa.Column("text_engine", sa.String(length=32), nullable=True),
        sa.Column("owner_user_id", sa.Integer(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(), nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.ForeignKeyConstraint(["root_id"], ["roots.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["owner_user_id"], ["users.id"]),
        sa.UniqueConstraint("root_id", "rel_path", name="uq_files_root_relpath"),
    )
    op.create_index("ix_files_root_id", "files", ["root_id"])
    op.create_index("ix_files_sha256", "files", ["sha256"])
    op.create_index("ix_files_file_size", "files", ["file_size"])
    op.create_index("ix_files_status", "files", ["status"])
    op.create_index("ix_files_text_status", "files", ["text_status"])

    # 3) share_file_items — file membership in a share (composite PK).
    op.create_table(
        "share_file_items",
        sa.Column("share_id", sa.Integer(), nullable=False),
        sa.Column("file_id", sa.Integer(), nullable=False),
        sa.Column(
            "sort_idx", sa.Integer(), nullable=False, server_default=sa.text("0")
        ),
        sa.ForeignKeyConstraint(["share_id"], ["shares.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["file_id"], ["files.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("share_id", "file_id"),
    )
    op.create_index("ix_share_file_items_share_id", "share_file_items", ["share_id"])

    # 4) file_fts — FTS5 trigram, SQLite only (mirrors photo_fts / 0020).
    #    New table starts empty, so no backfill. Non-SQLite backends skip
    #    it; app.fts feature-detects and takes the no-FTS branch there.
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        bind.exec_driver_sql(
            "CREATE VIRTUAL TABLE file_fts USING fts5(text, tokenize='trigram')"
        )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        bind.exec_driver_sql("DROP TABLE IF EXISTS file_fts")
    op.drop_index("ix_share_file_items_share_id", table_name="share_file_items")
    op.drop_table("share_file_items")
    for idx in (
        "ix_files_text_status", "ix_files_status", "ix_files_file_size",
        "ix_files_sha256", "ix_files_root_id",
    ):
        op.drop_index(idx, table_name="files")
    op.drop_table("files")
    op.drop_column("roots", "kind")

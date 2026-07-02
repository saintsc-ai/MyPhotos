"""file_text — extracted document text for content search

Revision ID: 0039_file_text
Revises: 0038_files_domain
Create Date: 2026-07-02

Holds the extracted plain text of a file (PDF/office/plain) separately from
the files row so listings stay lean. file_fts composes filename + rel_path +
this body (see app.fts). One row per file, written by the index_file_generic
job's extraction step.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0039_file_text"
down_revision: Union[str, None] = "0038_files_domain"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "file_text",
        sa.Column("file_id", sa.Integer(), primary_key=True),
        sa.Column("body", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(["file_id"], ["files.id"], ondelete="CASCADE"),
    )


def downgrade() -> None:
    op.drop_table("file_text")

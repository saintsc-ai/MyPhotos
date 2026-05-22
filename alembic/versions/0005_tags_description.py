"""tags + photo_tags + taken_at_original + description

Revision ID: 0005_tags_description
Revises: 0004_ratings_comments
Create Date: 2026-05-23 10:00:00

"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0005_tags_description"
down_revision: Union[str, None] = "0004_ratings_comments"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Photo extensions
    op.add_column("photos", sa.Column("taken_at_original", sa.DateTime, nullable=True))
    op.add_column("photos", sa.Column("description", sa.Text, nullable=True))

    # Tags
    op.create_table(
        "tags",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("name", sa.String(64), nullable=False, unique=True),
        sa.Column(
            "created_at",
            sa.DateTime,
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
    )

    op.create_table(
        "photo_tags",
        sa.Column(
            "photo_id",
            sa.Integer,
            sa.ForeignKey("photos.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "tag_id",
            sa.Integer,
            sa.ForeignKey("tags.id", ondelete="CASCADE"),
            primary_key=True,
        ),
    )
    op.create_index("ix_photo_tags_tag_id", "photo_tags", ["tag_id"])


def downgrade() -> None:
    op.drop_index("ix_photo_tags_tag_id", table_name="photo_tags")
    op.drop_table("photo_tags")
    op.drop_table("tags")
    op.drop_column("photos", "description")
    op.drop_column("photos", "taken_at_original")

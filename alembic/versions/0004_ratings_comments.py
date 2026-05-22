"""photo_ratings + photo_comments

Revision ID: 0004_ratings_comments
Revises: 0003_shares
Create Date: 2026-05-22 19:00:00

"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0004_ratings_comments"
down_revision: Union[str, None] = "0003_shares"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "photo_ratings",
        sa.Column(
            "photo_id", sa.Integer,
            sa.ForeignKey("photos.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "user_id", sa.Integer,
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("rating", sa.Integer, nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime,
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.CheckConstraint("rating BETWEEN 1 AND 5", name="ck_rating_range"),
    )

    op.create_table(
        "photo_comments",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column(
            "photo_id", sa.Integer,
            sa.ForeignKey("photos.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "user_id", sa.Integer,
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("body", sa.Text, nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime,
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime,
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
    )
    op.create_index(
        "ix_photo_comments_photo_id", "photo_comments", ["photo_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_photo_comments_photo_id", table_name="photo_comments")
    op.drop_table("photo_comments")
    op.drop_table("photo_ratings")

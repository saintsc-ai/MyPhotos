"""shares table (public share links)

Revision ID: 0003_shares
Revises: 0002_users
Create Date: 2026-05-22 18:30:00

"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0003_shares"
down_revision: Union[str, None] = "0002_users"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "shares",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("token", sa.String(64), nullable=False, unique=True),
        sa.Column(
            "photo_id",
            sa.Integer,
            sa.ForeignKey("photos.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("password_hash", sa.String(128), nullable=True),
        sa.Column("title", sa.String(256), nullable=True),
        sa.Column("expires_at", sa.DateTime, nullable=True),
        sa.Column(
            "view_count", sa.Integer, nullable=False, server_default=sa.text("0")
        ),
        sa.Column(
            "created_by_user_id", sa.Integer, sa.ForeignKey("users.id"), nullable=True
        ),
        sa.Column(
            "created_at",
            sa.DateTime,
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column("revoked_at", sa.DateTime, nullable=True),
    )
    op.create_index("ix_shares_token", "shares", ["token"])


def downgrade() -> None:
    op.drop_index("ix_shares_token", table_name="shares")
    op.drop_table("shares")

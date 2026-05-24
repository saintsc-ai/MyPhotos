"""shares: optional max-downloads cap + download counter

Revision ID: 0008_share_download_limit
Revises: 0007_share_items
Create Date: 2026-05-24 02:30:00
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0008_share_download_limit"
down_revision: Union[str, None] = "0007_share_items"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("shares") as batch:
        batch.add_column(
            sa.Column("max_downloads", sa.Integer, nullable=True)
        )
        batch.add_column(
            sa.Column(
                "download_count",
                sa.Integer,
                nullable=False,
                server_default="0",
            )
        )


def downgrade() -> None:
    with op.batch_alter_table("shares") as batch:
        batch.drop_column("download_count")
        batch.drop_column("max_downloads")

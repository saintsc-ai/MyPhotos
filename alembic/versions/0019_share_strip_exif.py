"""Add shares.strip_exif for optional EXIF GPS stripping on public download

Revision ID: 0019_share_strip_exif
Revises: 0018_uploads_pending
Create Date: 2026-05-27 18:00:00

The public share endpoint streams the original file bytes as-is — GPS
coordinates embedded in EXIF leak the home location to anyone with the
link. Adding an opt-in flag on the share row; when set, the JPEG
download path scrubs GPS and other identifying EXIF before streaming.

Default is False to preserve current behaviour for existing shares.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0019_share_strip_exif"
down_revision: Union[str, None] = "0018_uploads_pending"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("shares") as batch:
        batch.add_column(
            sa.Column(
                "strip_exif", sa.Boolean(), nullable=False, server_default=sa.false()
            )
        )


def downgrade() -> None:
    with op.batch_alter_table("shares") as batch:
        batch.drop_column("strip_exif")

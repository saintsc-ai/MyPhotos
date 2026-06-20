"""Add source / estimated_from / estimated_at to photo_locations.

Revision ID: 0035_photo_location_source
Revises: 0034_photo_object
Create Date: 2026-06-18

Lets the lightbox/map distinguish between GPS pulled off the file
('exif') and coordinates inferred by location_estimator from anchor
photos in the same / parent folder ('estimated'). Reserves 'user'
for an explicit "I'll type the coordinates myself" UI we haven't
shipped yet.

Backfill: NONE. NULL source on legacy rows is read as 'exif' at
query time — same trick the photo_faces.source migration used.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0035_photo_location_source"
down_revision: Union[str, None] = "0034_photo_object"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "photo_locations",
        sa.Column("source", sa.String(length=16), nullable=True),
    )
    op.add_column(
        "photo_locations",
        sa.Column("estimated_from_photo_ids", sa.Text(), nullable=True),
    )
    op.add_column(
        "photo_locations",
        sa.Column("estimated_at", sa.DateTime(), nullable=True),
    )
    op.create_index(
        "ix_photo_locations_source", "photo_locations", ["source"]
    )


def downgrade() -> None:
    op.drop_index("ix_photo_locations_source", table_name="photo_locations")
    op.drop_column("photo_locations", "estimated_at")
    op.drop_column("photo_locations", "estimated_from_photo_ids")
    op.drop_column("photo_locations", "source")

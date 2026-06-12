"""Add photo_objects table — spatial YOLO detections with bbox.

Revision ID: 0034_photo_object
Revises: 0033_photo_face_source
Create Date: 2026-06-13

Until now YOLO's output was only persisted as PhotoAutoTag rows (a
deduped per-photo set of class labels). The detector knows the bbox
of each instance, but we threw that away after NMS. This table keeps
one row per detection so the lightbox can overlay boxes on the image,
parallel to photo_faces.

Backfill policy: NONE. Existing classified photos stay tag-only.
The lightbox just shows zero object boxes for them. New classify_ml
runs (and admin re-requests of the objects stage) populate the new
table. This matches the user's decision to avoid the multi-hour cost
of re-running YOLO on the existing ~100k library.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0034_photo_object"
down_revision: Union[str, None] = "0033_photo_face_source"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "photo_objects",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("photo_id", sa.Integer(), nullable=False),
        sa.Column("label", sa.String(length=64), nullable=False),
        sa.Column("bbox_json", sa.Text(), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column(
            "source",
            sa.String(length=16),
            nullable=False,
            server_default="detector",
        ),
        sa.Column(
            "indexed_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.ForeignKeyConstraint(
            ["photo_id"], ["photos.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_photo_objects_photo_id", "photo_objects", ["photo_id"]
    )
    op.create_index(
        "ix_photo_objects_label", "photo_objects", ["label"]
    )


def downgrade() -> None:
    op.drop_index("ix_photo_objects_label", table_name="photo_objects")
    op.drop_index("ix_photo_objects_photo_id", table_name="photo_objects")
    op.drop_table("photo_objects")

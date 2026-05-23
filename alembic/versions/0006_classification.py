"""classify_status + tag source + photo_embeddings + faces

Revision ID: 0006_classification
Revises: 0005_tags_description
Create Date: 2026-05-23 11:00:00

"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0006_classification"
down_revision: Union[str, None] = "0005_tags_description"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Photo lifecycle: ML classification status independent of EXIF/thumb.
    op.add_column(
        "photos",
        sa.Column(
            "classify_status",
            sa.String(16),
            nullable=False,
            server_default=sa.text("'pending'"),
        ),
    )
    op.create_index(
        "ix_photos_classify_status", "photos", ["classify_status"]
    )

    # Tag origin discrimination — preserve user tags as 'user' on upgrade.
    op.add_column(
        "tags",
        sa.Column(
            "source",
            sa.String(16),
            nullable=False,
            server_default=sa.text("'user'"),
        ),
    )
    op.create_index("ix_tags_source", "tags", ["source"])

    # CLIP-class embedding (Round 2; table created now so the worker can
    # populate it during the same backfill run as YOLO).
    op.create_table(
        "photo_embeddings",
        sa.Column(
            "photo_id",
            sa.Integer,
            sa.ForeignKey("photos.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("model", sa.String(64), nullable=False),
        sa.Column("vector", sa.LargeBinary, nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime,
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
    )

    # Face clusters (named groups). Created before photo_faces because
    # photo_faces.cluster_id references it.
    op.create_table(
        "face_clusters",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("label", sa.String(64), nullable=True),
        sa.Column("centroid", sa.LargeBinary, nullable=True),
        sa.Column(
            "face_count", sa.Integer, nullable=False, server_default=sa.text("0")
        ),
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

    # Detected faces (Round 3 will populate; table ready now).
    op.create_table(
        "photo_faces",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column(
            "photo_id",
            sa.Integer,
            sa.ForeignKey("photos.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("bbox_json", sa.Text, nullable=False),
        sa.Column("embedding", sa.LargeBinary, nullable=False),
        sa.Column(
            "cluster_id",
            sa.Integer,
            sa.ForeignKey("face_clusters.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("confidence", sa.Float, nullable=False),
        sa.Column(
            "indexed_at",
            sa.DateTime,
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
    )
    op.create_index("ix_photo_faces_photo_id", "photo_faces", ["photo_id"])
    op.create_index("ix_photo_faces_cluster_id", "photo_faces", ["cluster_id"])


def downgrade() -> None:
    op.drop_index("ix_photo_faces_cluster_id", table_name="photo_faces")
    op.drop_index("ix_photo_faces_photo_id", table_name="photo_faces")
    op.drop_table("photo_faces")
    op.drop_table("face_clusters")
    op.drop_table("photo_embeddings")
    op.drop_index("ix_tags_source", table_name="tags")
    op.drop_column("tags", "source")
    op.drop_index("ix_photos_classify_status", table_name="photos")
    op.drop_column("photos", "classify_status")

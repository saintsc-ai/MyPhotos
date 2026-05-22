"""initial schema (roots, photos, photo_locations, jobs)

Revision ID: 0001_initial
Revises:
Create Date: 2026-05-22 00:00:00

"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0001_initial"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "roots",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("label", sa.String(64), nullable=False, unique=True),
        sa.Column("abs_path", sa.Text, nullable=False),
        sa.Column("readonly", sa.Boolean, nullable=False, server_default=sa.text("1")),
        sa.Column("enabled", sa.Boolean, nullable=False, server_default=sa.text("1")),
        sa.Column("scan_interval", sa.Integer, nullable=False, server_default=sa.text("86400")),
        sa.Column("last_full_scan", sa.DateTime, nullable=True),
        sa.Column("last_event_at", sa.DateTime, nullable=True),
        sa.Column("notes", sa.Text, nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime,
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
    )

    op.create_table(
        "photos",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column(
            "root_id",
            sa.Integer,
            sa.ForeignKey("roots.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("rel_path", sa.Text, nullable=False),
        sa.Column("filename", sa.String(512), nullable=False),
        sa.Column("ext", sa.String(16), nullable=False),
        sa.Column("media_kind", sa.String(16), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=True),
        sa.Column("file_size", sa.BigInteger, nullable=True),
        sa.Column("mtime", sa.DateTime, nullable=True),
        sa.Column("content_signature", sa.String(64), nullable=True),
        sa.Column("taken_at", sa.DateTime, nullable=True),
        sa.Column("width", sa.Integer, nullable=True),
        sa.Column("height", sa.Integer, nullable=True),
        sa.Column("camera_make", sa.String(64), nullable=True),
        sa.Column("camera_model", sa.String(128), nullable=True),
        sa.Column("lens", sa.String(128), nullable=True),
        sa.Column("iso", sa.Integer, nullable=True),
        sa.Column("fnumber", sa.Float, nullable=True),
        sa.Column("exposure", sa.String(32), nullable=True),
        sa.Column("focal_length", sa.Float, nullable=True),
        sa.Column("orientation", sa.Integer, nullable=True),
        sa.Column("duration_seconds", sa.Float, nullable=True),
        sa.Column("burst_uuid", sa.String(64), nullable=True),
        sa.Column("companion_id", sa.BigInteger, nullable=True),
        sa.Column("exif_status", sa.String(16), nullable=False, server_default=sa.text("'pending'")),
        sa.Column("exif_extractor", sa.String(16), nullable=True),
        sa.Column("exif_error", sa.Text, nullable=True),
        sa.Column("thumb_status", sa.String(16), nullable=False, server_default=sa.text("'pending'")),
        sa.Column("thumb_error", sa.Text, nullable=True),
        sa.Column("status", sa.String(16), nullable=False, server_default=sa.text("'active'")),
        sa.Column(
            "indexed_at",
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
        sa.UniqueConstraint("root_id", "rel_path", name="uq_photos_root_relpath"),
    )
    op.create_index("ix_photos_sha256", "photos", ["sha256"])
    op.create_index("ix_photos_taken_at", "photos", ["taken_at"])
    op.create_index("ix_photos_burst_uuid", "photos", ["burst_uuid"])
    op.create_index("ix_photos_status_taken", "photos", ["status", "taken_at"])
    op.create_index("ix_photos_exif_status", "photos", ["exif_status"])
    op.create_index("ix_photos_thumb_status", "photos", ["thumb_status"])

    op.create_table(
        "photo_locations",
        sa.Column(
            "photo_id",
            sa.BigInteger,
            sa.ForeignKey("photos.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("latitude", sa.Float, nullable=False),
        sa.Column("longitude", sa.Float, nullable=False),
        sa.Column("altitude", sa.Float, nullable=True),
        sa.CheckConstraint("latitude BETWEEN -90 AND 90", name="ck_latitude_range"),
        sa.CheckConstraint("longitude BETWEEN -180 AND 180", name="ck_longitude_range"),
    )
    op.create_index(
        "ix_photo_locations_lat_lon", "photo_locations", ["latitude", "longitude"]
    )

    op.create_table(
        "jobs",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("payload", sa.Text, nullable=False),
        sa.Column("priority", sa.Integer, nullable=False, server_default=sa.text("0")),
        sa.Column("status", sa.String(16), nullable=False, server_default=sa.text("'queued'")),
        sa.Column("claim_token", sa.String(36), nullable=True),
        sa.Column("attempts", sa.Integer, nullable=False, server_default=sa.text("0")),
        sa.Column("last_error", sa.Text, nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime,
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column("started_at", sa.DateTime, nullable=True),
        sa.Column("finished_at", sa.DateTime, nullable=True),
    )
    op.create_index("ix_jobs_status_priority_id", "jobs", ["status", "priority", "id"])
    op.create_index("ix_jobs_claim_token", "jobs", ["claim_token"])


def downgrade() -> None:
    op.drop_index("ix_jobs_claim_token", table_name="jobs")
    op.drop_index("ix_jobs_status_priority_id", table_name="jobs")
    op.drop_table("jobs")

    op.drop_index("ix_photo_locations_lat_lon", table_name="photo_locations")
    op.drop_table("photo_locations")

    op.drop_index("ix_photos_thumb_status", table_name="photos")
    op.drop_index("ix_photos_exif_status", table_name="photos")
    op.drop_index("ix_photos_status_taken", table_name="photos")
    op.drop_index("ix_photos_burst_uuid", table_name="photos")
    op.drop_index("ix_photos_taken_at", table_name="photos")
    op.drop_index("ix_photos_sha256", table_name="photos")
    op.drop_table("photos")

    op.drop_table("roots")

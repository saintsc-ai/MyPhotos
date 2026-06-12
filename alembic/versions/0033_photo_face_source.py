"""Add photo_faces.source to distinguish detector vs user-added faces.

Revision ID: 0033_photo_face_source
Revises: 0032_jobs_photo_id
Create Date: 2026-06-12

The lightbox 얼굴 추가 button lets an admin draw a missed face and
embed it (POST /api/admin/ml/faces). Until now those rows were
indistinguishable from YuNet auto-detections, so a re-run of
run_detect_faces wiped them via _clear_existing_faces() — destroying
real annotation work.

source = 'detector' | 'user' | NULL. NULL on rows that pre-date this
column; read-side code treats NULL as 'detector' since the only way to
land here without this column existing was the auto-detection path.
The clear-before-rerun path now filters to source != 'user' so manual
annotations survive across re-detections, and the detector's own
output gets IoU-deduped against any surviving user box so we don't end
up with two rows for the same face on the same photo.

SQLite + MariaDB + PostgreSQL all support plain ADD COLUMN of a
nullable String — no dialect branching.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0033_photo_face_source"
down_revision: Union[str, None] = "0032_jobs_photo_id"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "photo_faces",
        sa.Column("source", sa.String(length=16), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("photo_faces", "source")

"""Split classify_status into per-stage ML status columns.

Revision ID: 0031_photo_ml_stage_status
Revises: 0030_photo_ocr
Create Date: 2026-06-12

The image (photo) is the key and each ML stage gets its own status column,
so stages can be requested/skipped/retried/counted independently:

  objects_status — YOLO object detection
  clip_status    — CLIP embedding + scene tags
  faces_status   — YuNet/SFace face detection
  (ocr_status already exists — OCR)

Each is pending | ok | failed | skipped, mirroring the old rolled-up
classify_status, which is KEPT as a maintained roll-up (ok when all three
are ok/skipped) so existing ML donuts / stats / filters keep working.

Backfill from classify_status: ok → all three ok, skipped → all three
skipped, anything else stays the column default 'pending' (so a prior
'failed' re-runs all stages — we can't tell which stage failed).
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0031_photo_ml_stage_status"
down_revision: Union[str, None] = "0030_photo_ocr"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_COLS = ("objects_status", "clip_status", "faces_status")


def upgrade() -> None:
    # Native ADD COLUMN (no table copy) — cheap even on a large photos table.
    for col in _COLS:
        op.add_column(
            "photos",
            sa.Column(col, sa.String(length=16), nullable=False,
                      server_default="pending"),
        )
    # Backfill from the existing rolled-up classify_status.
    op.execute(
        "UPDATE photos SET objects_status='ok', clip_status='ok', "
        "faces_status='ok' WHERE classify_status='ok'"
    )
    op.execute(
        "UPDATE photos SET objects_status='skipped', clip_status='skipped', "
        "faces_status='skipped' WHERE classify_status='skipped'"
    )
    for col in _COLS:
        op.create_index(f"ix_photos_{col}", "photos", [col])


def downgrade() -> None:
    for col in _COLS:
        op.drop_index(f"ix_photos_{col}", table_name="photos")
    with op.batch_alter_table("photos") as batch:
        for col in _COLS:
            batch.drop_column(col)

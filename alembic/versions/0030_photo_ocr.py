"""Add photos.ocr_text + ocr_status for searchable OCR text.

Revision ID: 0030_photo_ocr
Revises: 0029_user_login_lockout
Create Date: 2026-06-04

OCR is an opt-in ML stage (like object/face classification): the admin
enqueues it, the ML worker runs RapidOCR on the photo thumbnail, and the
extracted text is stored in ocr_text and folded into the FTS5 search
index. ocr_status is NULL until attempted (so a fresh column doesn't
imply every existing photo is queued):

  NULL     — not attempted
  pending  — enqueued
  ok       — text found
  empty    — ran, no text detected
  failed   — OCR error
  skipped  — not an image / no thumbnail
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0030_photo_ocr"
down_revision: Union[str, None] = "0029_user_login_lockout"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("photos") as batch:
        batch.add_column(sa.Column("ocr_text", sa.Text(), nullable=True))
        batch.add_column(sa.Column("ocr_status", sa.String(length=16), nullable=True))
    op.create_index("ix_photos_ocr_status", "photos", ["ocr_status"])


def downgrade() -> None:
    op.drop_index("ix_photos_ocr_status", table_name="photos")
    with op.batch_alter_table("photos") as batch:
        batch.drop_column("ocr_status")
        batch.drop_column("ocr_text")

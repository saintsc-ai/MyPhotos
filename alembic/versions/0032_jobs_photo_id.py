"""Add jobs.photo_id + composite index for per-photo dedup.

Revision ID: 0032_jobs_photo_id
Revises: 0031_photo_ml_stage_status
Create Date: 2026-06-12

User-visible problem:
    Pressing 관리 → ML 자동 분류 → "분류 시작" twice for the same photo
    used to insert two `classify_ml` jobs into the queue. The worker
    reads the photo's per-stage status columns when it picks each job
    up, so the second job's stages are all already 'ok' and bail
    cheaply — but the queue and progress donut still inflate, and the
    user sees a wrong picture of how much work is left.

What this migration enables:
    A new `photo_id` column on `jobs` plus a composite index
    (kind, photo_id, status) lets `enqueue_unique_for_photo()` do a
    cheap pre-INSERT check —
        SELECT 1 FROM jobs
        WHERE kind=? AND photo_id=? AND status IN ('queued','running')
    — and skip the INSERT when there's already a live job for the
    same photo. The worker still re-reads the photo's stage status on
    pickup, so any newly-toggled stage on the existing job lands in
    the next pass automatically.

Notes:
    - NULL on non-photo jobs (discover_root, dedup_cleanup, …). Most
      DBs skip NULL entries in composite indexes, keeping the index
      tight (per-photo entries only).
    - Existing rows get photo_id = NULL via the standard ADD COLUMN
      default. We don't backfill from payload JSON — natural-rotation
      is enough (new rows fill it; queue empties over time).
    - SQLite + MariaDB + PostgreSQL all support plain ADD COLUMN +
      CREATE INDEX so no dialect branching needed.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0032_jobs_photo_id"
down_revision: Union[str, None] = "0031_photo_ml_stage_status"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "jobs",
        sa.Column("photo_id", sa.Integer(), nullable=True),
    )
    op.create_index(
        "ix_jobs_kind_photo_status",
        "jobs",
        ["kind", "photo_id", "status"],
    )


def downgrade() -> None:
    op.drop_index("ix_jobs_kind_photo_status", table_name="jobs")
    op.drop_column("jobs", "photo_id")

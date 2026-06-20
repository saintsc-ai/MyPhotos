"""Add photo_work.stage_params — per-stage runtime parameters.

Revision ID: 0037_photo_work_stage_params
Revises: 0036_photo_work
Create Date: 2026-06-21

`stages` only carries status. Some stages take a parameter from the
trigger (e.g. estimate_location's threshold_seconds — user picks 6h
vs 7d in the admin UI). Without this column those params got dropped
on the floor and the handler silently used its hard-coded default,
which made re-triggering at a wider threshold do nothing visible.

Format: JSON object {stage_name: {key: value, ...}}. Caller writes
into it via enqueue_stage(..., params=...); the dispatcher passes
the stage's slice to the handler.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0037_photo_work_stage_params"
down_revision: Union[str, None] = "0036_photo_work"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "photo_work",
        sa.Column(
            "stage_params", sa.Text(), nullable=False, server_default="{}",
        ),
    )


def downgrade() -> None:
    op.drop_column("photo_work", "stage_params")

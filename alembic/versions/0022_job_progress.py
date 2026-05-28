"""Add progress columns to jobs

Revision ID: 0022_job_progress
Revises: 0021_user_display_name
Create Date: 2026-05-28 19:00:00

Long-running jobs (dedup auto-cleanup, future ML reruns) need to expose
progress to the admin UI without forcing each job kind to invent its
own status field. Two ints — done / total — are enough for a generic
"X / Y" progress bar. total=0 means "not yet known" (the job will set
it after its initial size query); done is incremented as work lands.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0022_job_progress"
down_revision: Union[str, None] = "0021_user_display_name"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "jobs",
        sa.Column("progress_done", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "jobs",
        sa.Column("progress_total", sa.Integer(), nullable=False, server_default="0"),
    )


def downgrade() -> None:
    with op.batch_alter_table("jobs") as batch:
        batch.drop_column("progress_total")
        batch.drop_column("progress_done")

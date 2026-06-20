"""Add photo_work table — photo-unit queue parallel to legacy jobs.

Revision ID: 0036_photo_work
Revises: 0035_photo_location_source
Create Date: 2026-06-21

Long-term replacement for the legacy `jobs` table's per-stage rows. A
single photo gets a single row here, with a JSON `stages` map that the
new dispatcher walks through. The legacy table stays in place during
the transition — both dispatchers run side by side.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0036_photo_work"
down_revision: Union[str, None] = "0035_photo_location_source"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "photo_work",
        sa.Column("photo_id", sa.Integer(), nullable=False),
        sa.Column("stages", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("priority", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("claim_token", sa.String(length=36), nullable=True),
        sa.Column("claimed_at", sa.DateTime(), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(), nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column(
            "updated_at", sa.DateTime(), nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.ForeignKeyConstraint(
            ["photo_id"], ["photos.id"], ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("photo_id"),
    )
    op.create_index(
        "ix_photo_work_claim", "photo_work",
        ["claim_token", "priority", "photo_id"],
    )
    op.create_index(
        "ix_photo_work_claimed_at", "photo_work", ["claimed_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_photo_work_claimed_at", table_name="photo_work")
    op.drop_index("ix_photo_work_claim", table_name="photo_work")
    op.drop_table("photo_work")

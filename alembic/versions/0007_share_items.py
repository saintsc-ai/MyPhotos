"""share_items: multi-photo share links

Revision ID: 0007_share_items
Revises: 0006_classification
Create Date: 2026-05-24 02:00:00

Backwards-compatible move from one-photo-per-share to N-photos-per-share.
Adds a `share_items` join table and backfills it from the existing
`shares.photo_id` column. `photo_id` stays around (nullable now) so an
older app build can still resolve single-photo shares while the new
build prefers `share_items`. New shares always write share_items rows
even when there's only one photo.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0007_share_items"
down_revision: Union[str, None] = "0006_classification"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "share_items",
        sa.Column(
            "share_id",
            sa.Integer,
            sa.ForeignKey("shares.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "photo_id",
            sa.Integer,
            sa.ForeignKey("photos.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("sort_idx", sa.Integer, nullable=False, server_default="0"),
        sa.PrimaryKeyConstraint("share_id", "photo_id", name="pk_share_items"),
    )
    op.create_index(
        "ix_share_items_share_id", "share_items", ["share_id"]
    )

    # Backfill: existing single-photo shares get one share_items row.
    op.execute(
        """
        INSERT INTO share_items (share_id, photo_id, sort_idx)
        SELECT id, photo_id, 0
        FROM shares
        WHERE photo_id IS NOT NULL
        """
    )

    # Make shares.photo_id nullable so future bulk shares don't have to
    # pick a "primary" photo. We keep the column for now to avoid a
    # destructive table-rebuild on SQLite — the runtime code reads
    # share_items first.
    with op.batch_alter_table("shares") as batch:
        batch.alter_column("photo_id", existing_type=sa.Integer, nullable=True)


def downgrade() -> None:
    with op.batch_alter_table("shares") as batch:
        batch.alter_column("photo_id", existing_type=sa.Integer, nullable=False)
    op.drop_index("ix_share_items_share_id", table_name="share_items")
    op.drop_table("share_items")

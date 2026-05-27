"""Backfill photos.visibility = 'inherit' for any rows where the
0015 batch recreate didn't apply the DEFAULT

Revision ID: 0016_backfill_visibility
Revises: 0015_photo_visibility
Create Date: 2026-05-27 16:00:00

The 0015 batch_alter_table flow on SQLite recreates the photos table
and is supposed to populate the new `visibility` column from the
server_default = 'inherit'. In practice some SQLite + alembic
combinations leave existing rows with NULL instead. With NULL,
`visibility = 'inherit'` evaluates to NULL (not TRUE) in the ACL
filter, so non-admin users see no photos at all.

This is a no-op for fresh installs (column already has 'inherit'
everywhere) and a one-shot fix for the upgrade path. The runtime
filter also defensively treats NULL as 'inherit' since 0015+ —
this migration is the data side of the same fix.
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op

revision: str = "0016_backfill_visibility"
down_revision: Union[str, None] = "0015_photo_visibility"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("UPDATE photos SET visibility = 'inherit' WHERE visibility IS NULL")


def downgrade() -> None:
    # No-op — the 0015 downgrade drops the column entirely, so there's
    # nothing to revert here.
    pass

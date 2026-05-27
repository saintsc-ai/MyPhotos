"""Add per-user permission flags (P1 of the access-control plan)

Revision ID: 0012_user_permission_flags
Revises: 0011_root_ignore_paths
Create Date: 2026-05-27 09:00:00

Adds four boolean flags to the users table so admins can scope what
each viewer is allowed to do. Defaults to FALSE (locked-down new
users), but existing rows get UPDATEd to TRUE to keep current
behavior unchanged — see docs/ACCESS_CONTROL_PLAN.md §3.1.

Phase P1 only touches users — root/folder/photo-level ACL come in
P2/P3/P4 with their own revisions.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0012_user_permission_flags"
down_revision: Union[str, None] = "0011_root_ignore_paths"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("users") as batch:
        batch.add_column(sa.Column(
            "can_upload", sa.Boolean(), nullable=False, server_default=sa.false(),
        ))
        batch.add_column(sa.Column(
            "can_delete", sa.Boolean(), nullable=False, server_default=sa.false(),
        ))
        batch.add_column(sa.Column(
            "can_share", sa.Boolean(), nullable=False, server_default=sa.false(),
        ))
        batch.add_column(sa.Column(
            "can_edit_meta_others", sa.Boolean(), nullable=False, server_default=sa.false(),
        ))

    # Liberal default for everyone already in the DB — they had every
    # capability before this revision, so flipping flags to TRUE keeps
    # them able to do the same things. New users created after this
    # revision land with server_default=FALSE (locked down by default).
    op.execute(
        "UPDATE users SET "
        "  can_upload = 1, "
        "  can_delete = 1, "
        "  can_share = 1, "
        "  can_edit_meta_others = 1"
    )


def downgrade() -> None:
    # WARNING: drops the permission state. Take a snapshot first.
    with op.batch_alter_table("users") as batch:
        batch.drop_column("can_edit_meta_others")
        batch.drop_column("can_share")
        batch.drop_column("can_delete")
        batch.drop_column("can_upload")

"""Add per-root ACL table (P2 of the access-control plan)

Revision ID: 0013_root_acl
Revises: 0012_user_permission_flags
Create Date: 2026-05-27 11:00:00

One row per (root, user) — when present, the row's `level` overrides
the default of `read` for that user on that root. No row = read
(backward compatible). See docs/ACCESS_CONTROL_PLAN.md §3.2.

Levels (most→least restrictive):
  hidden / read / interact / contribute / manage

Admin bypasses every ACL — they don't need rows here, and rows for
admin users are harmless but ignored at query time.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0013_root_acl"
down_revision: Union[str, None] = "0012_user_permission_flags"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "root_acl",
        sa.Column(
            "root_id", sa.Integer(),
            sa.ForeignKey("roots.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column(
            "user_id", sa.Integer(),
            sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column(
            "level", sa.String(length=16), nullable=False,
        ),
        sa.Column(
            "created_at", sa.DateTime(),
            server_default=sa.func.current_timestamp(), nullable=False,
        ),
        sa.PrimaryKeyConstraint("root_id", "user_id", name="pk_root_acl"),
        sa.CheckConstraint(
            "level IN ('hidden','read','interact','contribute','manage')",
            name="ck_root_acl_level",
        ),
    )
    op.create_index(
        "ix_root_acl_user", "root_acl", ["user_id"],
    )


def downgrade() -> None:
    # WARNING: drops every ACL row.
    op.drop_index("ix_root_acl_user", table_name="root_acl")
    op.drop_table("root_acl")

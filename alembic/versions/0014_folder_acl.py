"""Add per-folder ACL table (P3 of the access-control plan)

Revision ID: 0014_folder_acl
Revises: 0013_root_acl
Create Date: 2026-05-27 13:00:00

Layered on top of root_acl: a folder_acl row overrides the root-level
default for a specific path prefix. The longest matching prefix wins,
and folder_acl entries can either tighten (root=read, folder=hidden)
or loosen (root=hidden, folder=read) the parent's level — see
docs/ACCESS_CONTROL_PLAN.md §2.3 for the priority rules.

path_prefix is stored with a trailing slash so SUBSTR / LIKE matches
unambiguously (e.g. 'family/private/' matches 'family/private/x.jpg'
but not 'family/private2/x.jpg').
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0014_folder_acl"
down_revision: Union[str, None] = "0013_root_acl"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "folder_acl",
        sa.Column(
            "root_id", sa.Integer(),
            sa.ForeignKey("roots.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column(
            "path_prefix", sa.Text(), nullable=False,
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
        sa.PrimaryKeyConstraint(
            "root_id", "path_prefix", "user_id", name="pk_folder_acl",
        ),
        sa.CheckConstraint(
            "level IN ('hidden','read','interact','contribute','manage')",
            name="ck_folder_acl_level",
        ),
    )
    op.create_index(
        "ix_folder_acl_user_root", "folder_acl", ["user_id", "root_id"],
    )


def downgrade() -> None:
    # WARNING: drops every folder ACL row.
    op.drop_index("ix_folder_acl_user_root", table_name="folder_acl")
    op.drop_table("folder_acl")

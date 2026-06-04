"""Add users.failed_login_count + locked_until for account lockout.

Revision ID: 0029_user_login_lockout
Revises: 0028_root_owner_from_subfolder
Create Date: 2026-06-04

Backs the per-account lockout: consecutive failed logins increment
failed_login_count; crossing the configured threshold sets locked_until
to a future time, and logins are refused until it passes. A successful
login clears both. Existing rows default to 0 / NULL (unlocked).
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0029_user_login_lockout"
down_revision: Union[str, None] = "0028_root_owner_from_subfolder"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("users") as batch:
        batch.add_column(
            sa.Column(
                "failed_login_count",
                sa.Integer(),
                nullable=False,
                server_default="0",
            )
        )
        batch.add_column(sa.Column("locked_until", sa.DateTime(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("users") as batch:
        batch.drop_column("locked_until")
        batch.drop_column("failed_login_count")

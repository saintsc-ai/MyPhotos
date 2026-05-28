"""Add display_name (human name) to users

Revision ID: 0021_user_display_name
Revises: 0020_photo_fts
Create Date: 2026-05-28 09:00:00

`username` is the login ID and is ASCII-only (Korean can't be typed
into it), so it can't say who the account actually belongs to. Add a
required `display_name` column for the real name (e.g. "홍길동").

Existing rows are backfilled with their username so the NOT NULL
constraint holds at upgrade time; the admin can rename them afterwards.
The temporary server_default is dropped so future INSERTs must supply a
value (the app's Pydantic schema requires a non-empty one).
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0021_user_display_name"
down_revision: Union[str, None] = "0020_photo_fts"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Add with a temporary server_default so the column is NOT NULL even
    # for rows that already exist, then backfill from username and drop
    # the default (SQLite needs a table rebuild for the drop — batch).
    op.add_column(
        "users",
        sa.Column(
            "display_name", sa.String(length=128),
            nullable=False, server_default="",
        ),
    )
    op.execute("UPDATE users SET display_name = username WHERE display_name = ''")
    with op.batch_alter_table("users") as batch:
        batch.alter_column("display_name", server_default=None)


def downgrade() -> None:
    with op.batch_alter_table("users") as batch:
        batch.drop_column("display_name")

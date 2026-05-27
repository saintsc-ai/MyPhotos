"""Add photos.trashed_by_user_id + audit_log table (P5 of access
control — trash isolation + audit log)

Revision ID: 0017_trash_isolation_audit
Revises: 0016_backfill_visibility
Create Date: 2026-05-27 17:30:00

Two pieces of P5:

1. photos.trashed_by_user_id — set when a viewer moves a photo to
   trash. The trash list endpoint filters to the caller's own
   deletions by default (admins can pass ?all=true to see everything).
   Legacy rows already in trash are NULL → only the admin "?all" view
   surfaces them.

2. audit_log — append-only record of who did what when. Indexed by
   timestamp and by (resource_type, resource_id). Stored username
   is denormalised so deleted users still show up in the log.

A scheduled purge keeps audit_log small (default: drop rows older
than 90 days), wired into the worker tick.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0017_trash_isolation_audit"
down_revision: Union[str, None] = "0016_backfill_visibility"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Same batch pattern as 0015 — named FK so SQLite recreate doesn't
    # raise "Constraint must have a name".
    with op.batch_alter_table("photos") as batch:
        batch.add_column(sa.Column(
            "trashed_by_user_id", sa.Integer(),
            sa.ForeignKey(
                "users.id",
                ondelete="SET NULL",
                name="fk_photos_trashed_by_user_id",
            ),
            nullable=True,
        ))

    op.create_table(
        "audit_log",
        sa.Column(
            "id", sa.Integer(), primary_key=True, autoincrement=True,
        ),
        sa.Column(
            "ts", sa.DateTime(),
            server_default=sa.func.current_timestamp(), nullable=False,
        ),
        sa.Column(
            "user_id", sa.Integer(),
            sa.ForeignKey(
                "users.id", ondelete="SET NULL",
                name="fk_audit_log_user_id",
            ),
            nullable=True,
        ),
        # Denormalised username so log rows survive user deletion.
        sa.Column("username", sa.String(length=64), nullable=True),
        sa.Column("action", sa.String(length=64), nullable=False),
        sa.Column("resource_type", sa.String(length=32), nullable=False),
        sa.Column("resource_id", sa.String(length=128), nullable=True),
        sa.Column("detail", sa.Text(), nullable=True),
    )
    op.create_index("ix_audit_log_ts", "audit_log", ["ts"])
    op.create_index(
        "ix_audit_log_user_ts", "audit_log", ["user_id", "ts"],
    )
    op.create_index(
        "ix_audit_log_resource",
        "audit_log",
        ["resource_type", "resource_id", "ts"],
    )


def downgrade() -> None:
    op.drop_index("ix_audit_log_resource", table_name="audit_log")
    op.drop_index("ix_audit_log_user_ts", table_name="audit_log")
    op.drop_index("ix_audit_log_ts", table_name="audit_log")
    op.drop_table("audit_log")
    with op.batch_alter_table("photos") as batch:
        batch.drop_column("trashed_by_user_id")

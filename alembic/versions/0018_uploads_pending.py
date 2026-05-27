"""Add uploads_pending — maps freshly uploaded files to the uploader
so the indexer can stamp Photo.owner_user_id when the row is created.

Revision ID: 0018_uploads_pending
Revises: 0017_trash_isolation_audit
Create Date: 2026-05-27 19:00:00

Why a separate table instead of stamping owner_user_id at upload time?

The upload endpoint only drops files on disk — actual Photo rows are
created later by the scanner / index_file job. The two are decoupled
on purpose so the catalog stays in sync even when files appear via
rsync, filesystem watcher, or external moves.

So we record (root_id, rel_path, user_id) on upload (rel_path is the
full POSIX path including filename, matching Photo.rel_path exactly).
The indexer's per-photo insert path looks for a matching pending row
and stamps owner_user_id + deletes the pending row in the same
transaction. Stale rows (file never indexed) are cleaned by a periodic
worker tick that drops rows older than 7 days.

Unique on (root_id, rel_path) — same path uploaded twice overwrites
the prior pending row (last writer wins). The upload endpoint already
disambiguates with a timestamp suffix when the destination exists.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0018_uploads_pending"
down_revision: Union[str, None] = "0017_trash_isolation_audit"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "uploads_pending",
        sa.Column(
            "id", sa.Integer(), primary_key=True, autoincrement=True,
        ),
        sa.Column(
            "root_id", sa.Integer(),
            sa.ForeignKey(
                "roots.id", ondelete="CASCADE",
                name="fk_uploads_pending_root_id",
            ),
            nullable=False,
        ),
        sa.Column("rel_path", sa.Text(), nullable=False),
        sa.Column(
            "user_id", sa.Integer(),
            sa.ForeignKey(
                "users.id", ondelete="SET NULL",
                name="fk_uploads_pending_user_id",
            ),
            nullable=True,
        ),
        sa.Column(
            "created_at", sa.DateTime(),
            server_default=sa.func.current_timestamp(), nullable=False,
        ),
        sa.UniqueConstraint(
            "root_id", "rel_path",
            name="uq_uploads_pending_path",
        ),
    )
    op.create_index(
        "ix_uploads_pending_created_at",
        "uploads_pending",
        ["created_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_uploads_pending_created_at", table_name="uploads_pending",
    )
    op.drop_table("uploads_pending")

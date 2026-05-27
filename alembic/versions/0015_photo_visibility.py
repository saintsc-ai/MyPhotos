"""Add photos.owner_user_id + photos.visibility (P4 of access control)

Revision ID: 0015_photo_visibility
Revises: 0014_folder_acl
Create Date: 2026-05-27 14:30:00

Per-photo override on top of root_acl / folder_acl. visibility=
'inherit' (default) falls through to the folder/root hierarchy;
'private' restricts to owner + admin regardless of any ACL grants;
'public' forces level=read on top, useful for re-exposing one photo
inside an otherwise hidden root.

owner_user_id is populated for new uploads (P1's upload endpoint
already runs as `require_can_upload`, so we know who's responsible).
Existing rows leave it NULL — those photos have no owner, so the
private-toggle is admin-only for them until a future migration
backfills ownership from upload audit logs (P5).
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0015_photo_visibility"
down_revision: Union[str, None] = "0014_folder_acl"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Two ALTER ADD COLUMNs in one batch. The CHECK on visibility lives
    # in the Photo model only — SQLite batch_alter_table can't reliably
    # add a named CHECK to an existing column via create_check_constraint
    # (it raises "Constraint must have a name" inside the batch
    # transaction), and the API + Pydantic layer already validates the
    # value before any INSERT/UPDATE, so the DB-level CHECK was belt-
    # and-suspenders.
    with op.batch_alter_table("photos") as batch:
        batch.add_column(sa.Column(
            "owner_user_id", sa.Integer(),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ))
        batch.add_column(sa.Column(
            "visibility", sa.String(length=16),
            nullable=False, server_default="inherit",
        ))
    # Partial index — most photos are owner=NULL (legacy uploads).
    # Sqlite supports the WHERE clause; the index payoff is for
    # "show me everything I uploaded" queries that may show up later.
    op.create_index(
        "ix_photos_owner",
        "photos",
        ["owner_user_id"],
        sqlite_where=sa.text("owner_user_id IS NOT NULL"),
    )


def downgrade() -> None:
    # WARNING: drops visibility + ownership info.
    op.drop_index("ix_photos_owner", table_name="photos")
    with op.batch_alter_table("photos") as batch:
        batch.drop_column("visibility")
        batch.drop_column("owner_user_id")

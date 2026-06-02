"""Add roots.owner_from_subfolder for path-based uploader attribution.

Revision ID: 0028_root_owner_from_subfolder
Revises: 0027_video_proxy
Create Date: 2026-06-03

When a root is fed by an external uploader (e.g. PhotoSync over SMB) the
files never pass through the authenticated /upload endpoint, so there's no
UploadPending row and the scanner leaves Photo.owner_user_id NULL ("no
recorded uploader"). With this flag on, the scanner instead reads the first
path segment under the root and, if it exactly matches a User.username,
attributes the photo to that user — so a folder layout like
``<root>/<username>/2026/06/IMG.jpg`` records the right uploader.

Opt-in per root (default false) so normal roots whose top folders are
years/albums (not usernames) are never affected.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0028_root_owner_from_subfolder"
down_revision: Union[str, None] = "0027_video_proxy"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("roots") as batch:
        batch.add_column(
            sa.Column(
                "owner_from_subfolder",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            )
        )


def downgrade() -> None:
    with op.batch_alter_table("roots") as batch:
        batch.drop_column("owner_from_subfolder")

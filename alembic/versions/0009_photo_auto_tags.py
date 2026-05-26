"""Split ML-generated tags into a separate photo_auto_tags table

Revision ID: 0009_photo_auto_tags
Revises: 0008_share_download_limit
Create Date: 2026-05-26 17:30:00

Previously every tag-photo link lived in photo_tags regardless of who
created it (user input vs ML classifier), with provenance recorded only
on the Tag row itself. That made the link table fragile in two ways:

  1. set_photo_tags() replaces a photo's entire row set, so saving any
     user edit nuked every ML-generated link on that photo.
  2. A tag added once by a user and later detected by ML would re-use
     the same Tag row, losing per-link provenance.

This migration introduces a dedicated photo_auto_tags table for ML
links, sharing the `tags` dictionary. Existing photo_tags rows whose
underlying tag has source LIKE 'auto-%' are moved into the new table
with their (yolo|clip|face) source preserved.

Confidence is left NULL for migrated rows — we never stored a score
in the old shape.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0009_photo_auto_tags"
down_revision: Union[str, None] = "0008_share_download_limit"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "photo_auto_tags",
        sa.Column(
            "photo_id",
            sa.Integer,
            sa.ForeignKey("photos.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "tag_id",
            sa.Integer,
            sa.ForeignKey("tags.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("source", sa.String(length=16), primary_key=True),
        sa.Column("confidence", sa.Float, nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime,
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
    )
    op.create_index("ix_photo_auto_tags_tag_id", "photo_auto_tags", ["tag_id"])
    op.create_index("ix_photo_auto_tags_source", "photo_auto_tags", ["source"])

    bind = op.get_bind()

    # Copy auto-* photo_tags rows into the new table, taking source from
    # the tag's source column. INSERT OR IGNORE so re-running the
    # migration on a partial state doesn't blow up on duplicate PKs
    # (SQLite syntax; MariaDB equivalent below).
    insert_sqlite = """
        INSERT OR IGNORE INTO photo_auto_tags (photo_id, tag_id, source)
        SELECT pt.photo_id, pt.tag_id, t.source
          FROM photo_tags pt
          JOIN tags t ON t.id = pt.tag_id
         WHERE t.source LIKE 'auto-%' OR t.source = 'face'
    """
    insert_mariadb = """
        INSERT IGNORE INTO photo_auto_tags (photo_id, tag_id, source)
        SELECT pt.photo_id, pt.tag_id, t.source
          FROM photo_tags pt
          JOIN tags t ON t.id = pt.tag_id
         WHERE t.source LIKE 'auto-%' OR t.source = 'face'
    """
    if bind.dialect.name == "sqlite":
        bind.execute(sa.text(insert_sqlite))
    else:
        bind.execute(sa.text(insert_mariadb))

    # Now drop the auto-* links from photo_tags so the user-tag table
    # is clean. We keep the Tag rows themselves around — the dictionary
    # is shared, and a user might want to manually attach what used to
    # be an ML-only label.
    bind.execute(
        sa.text(
            """
            DELETE FROM photo_tags
             WHERE tag_id IN (
                 SELECT id FROM tags
                  WHERE source LIKE 'auto-%' OR source = 'face'
             )
            """
        )
    )


def downgrade() -> None:
    bind = op.get_bind()
    # Re-merge auto-tag links back into photo_tags, then drop the
    # dedicated table. Same INSERT-OR-IGNORE pattern.
    if bind.dialect.name == "sqlite":
        bind.execute(
            sa.text(
                "INSERT OR IGNORE INTO photo_tags (photo_id, tag_id) "
                "SELECT photo_id, tag_id FROM photo_auto_tags"
            )
        )
    else:
        bind.execute(
            sa.text(
                "INSERT IGNORE INTO photo_tags (photo_id, tag_id) "
                "SELECT photo_id, tag_id FROM photo_auto_tags"
            )
        )
    op.drop_index("ix_photo_auto_tags_source", table_name="photo_auto_tags")
    op.drop_index("ix_photo_auto_tags_tag_id", table_name="photo_auto_tags")
    op.drop_table("photo_auto_tags")

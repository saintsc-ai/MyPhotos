"""Add photos.proxy_status + proxy_error for web-playable video proxies.

Revision ID: 0027_video_proxy
Revises: 0026_rel_path_binary_collation
Create Date: 2026-06-02

The browser plays the original video file as-is (no transcoding), so
formats it can't decode — HEVC/H.265 (common iPhone .mov/.mp4),
.mkv/.avi/.3gp containers — won't play. This adds a lazily-built H.264
proxy: when playback fails the API enqueues a `transcode_proxy` job that
writes a 1080p H.264/AAC copy under data/proxies/<sha>.mp4, and
`/api/photos/{id}/video` then serves that proxy.

Columns (NULL = never needed a proxy / playable as-is):
  - proxy_status: pending | running | done | failed  (NULL otherwise)
  - proxy_error:  last ffmpeg error when failed
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0027_video_proxy"
down_revision: Union[str, None] = "0026_rel_path_binary_collation"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("photos") as batch:
        batch.add_column(sa.Column("proxy_status", sa.String(length=16), nullable=True))
        batch.add_column(sa.Column("proxy_error", sa.Text(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("photos") as batch:
        batch.drop_column("proxy_error")
        batch.drop_column("proxy_status")

"""Index photos.file_size for the file-size search filter

Revision ID: 0010_photo_file_size_index
Revises: 0009_photo_auto_tags
Create Date: 2026-05-26 19:00:00

The 헤더 검색의 "파일 크기 ≥ N KB" 필터는 list_photos /
in-cell / date_histogram 모두 photos.file_size > ? 또는 BETWEEN
서술어를 추가하는데 인덱스가 없어 전체 테이블 스캔.

Single-column ascending index — cheap to maintain (file_size never
changes for an existing row), tiny on disk, but turns a ~10만-row
seq scan into an index range scan when the filter is selective.
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op

revision: str = "0010_photo_file_size_index"
down_revision: Union[str, None] = "0009_photo_auto_tags"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_index("ix_photos_file_size", "photos", ["file_size"])


def downgrade() -> None:
    op.drop_index("ix_photos_file_size", table_name="photos")

"""SQLAlchemy ORM models for the catalog.

Schema rationale:
- `roots` carries (label, abs_path). Photos reference roots by id, but
  abs_path is the only thing tied to a specific host — so porting between
  hosts means updating roots.abs_path, not touching photos.
- `photos` separates `exif_status` and `thumb_status` so partial failures
  (Pentax MakerNote choking one extractor, HEIC missing on a host, etc.)
  don't lose the row. The row is the source of truth that "the file exists";
  metadata and thumbs catch up asynchronously.
- `photo_locations` keeps GPS out of `photos` so the spatial index isn't
  bloated by photos without coordinates. Most personal libraries are mixed.
- `jobs` is a simple DB-backed queue. Claim pattern uses claim_token because
  SQLite has no SELECT ... FOR UPDATE SKIP LOCKED.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class Root(Base):
    __tablename__ = "roots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    label: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    abs_path: Mapped[str] = mapped_column(Text, nullable=False)
    readonly: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    scan_interval: Mapped[int] = mapped_column(Integer, nullable=False, default=86_400)
    last_full_scan: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    last_event_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.current_timestamp()
    )

    photos: Mapped[list["Photo"]] = relationship(
        back_populates="root", cascade="all, delete-orphan"
    )


class Photo(Base):
    __tablename__ = "photos"

    # NOTE: Integer (not BigInteger). SQLite ROWID aliasing only kicks in for
    # the literal type INTEGER, so BigInteger PKs do NOT auto-increment.
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    root_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("roots.id", ondelete="CASCADE"), nullable=False
    )
    # POSIX-style relative path, NFC-normalized. Stored without leading slash.
    rel_path: Mapped[str] = mapped_column(Text, nullable=False)
    filename: Mapped[str] = mapped_column(String(512), nullable=False)
    ext: Mapped[str] = mapped_column(String(16), nullable=False)
    media_kind: Mapped[str] = mapped_column(String(16), nullable=False)  # image | video

    # Identity / change detection
    sha256: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, index=True)
    file_size: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    mtime: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    # Cheap signature for incremental scan: usually f"{size}:{mtime_ns}".
    content_signature: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)

    # Capture metadata
    taken_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True, index=True)
    width: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    height: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    camera_make: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    camera_model: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    lens: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    iso: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    fnumber: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    exposure: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    focal_length: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    orientation: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    duration_seconds: Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    # Grouping: Live Photos / bursts. Same UUID groups multiple files together.
    burst_uuid: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, index=True)
    # Direct 1:1 companion (e.g. HEIC <-> MOV pair). Points to another photo id.
    companion_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    # Pipeline status — each stage tracks its own outcome.
    # values: pending | ok | partial | failed | skipped
    exif_status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    exif_extractor: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)
    exif_error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    thumb_status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    thumb_error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # Lifecycle
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="active"
    )  # active | trashed | missing
    indexed_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.current_timestamp()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
        server_default=func.current_timestamp(),
        onupdate=func.current_timestamp(),
    )

    root: Mapped[Root] = relationship(back_populates="photos")
    location: Mapped[Optional["PhotoLocation"]] = relationship(
        back_populates="photo", uselist=False, cascade="all, delete-orphan"
    )

    __table_args__ = (
        UniqueConstraint("root_id", "rel_path", name="uq_photos_root_relpath"),
        Index("ix_photos_status_taken", "status", "taken_at"),
        Index("ix_photos_exif_status", "exif_status"),
        Index("ix_photos_thumb_status", "thumb_status"),
    )


class PhotoLocation(Base):
    """Separate table for GPS data — kept out of `photos` so most rows stay narrow."""

    __tablename__ = "photo_locations"

    photo_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("photos.id", ondelete="CASCADE"), primary_key=True
    )
    latitude: Mapped[float] = mapped_column(Float, nullable=False)
    longitude: Mapped[float] = mapped_column(Float, nullable=False)
    altitude: Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    photo: Mapped[Photo] = relationship(back_populates="location")

    __table_args__ = (
        CheckConstraint("latitude BETWEEN -90 AND 90", name="ck_latitude_range"),
        CheckConstraint("longitude BETWEEN -180 AND 180", name="ck_longitude_range"),
        Index("ix_photo_locations_lat_lon", "latitude", "longitude"),
    )


class User(Base):
    """Login account. Simple username + bcrypt hash; sessions live in a
    signed cookie via Starlette's SessionMiddleware, so no `sessions` table.

    On first startup `auth.ensure_default_admin` seeds an admin / admin user
    if the table is empty. The frontend prompts to change the password while
    the hash still matches the seed value.
    """

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    is_admin: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.current_timestamp()
    )
    last_login_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)


class Share(Base):
    """Public, token-addressable view of one photo.

    The token alone is enough to view a passwordless share, so URL secrecy
    is the only barrier — keep tokens random and long. Optional password
    adds a second factor for sensitive photos. `revoked_at` is a kill
    switch independent of `expires_at`.
    """

    __tablename__ = "shares"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    token: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    photo_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("photos.id", ondelete="CASCADE"), nullable=False
    )
    password_hash: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    title: Mapped[Optional[str]] = mapped_column(String(256), nullable=True)
    expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    view_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_by_user_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("users.id"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.current_timestamp()
    )
    revoked_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)


class Job(Base):
    """DB-backed work queue.

    Claim pattern (no SKIP LOCKED in SQLite):
      1. Worker generates a claim_token (UUID4).
      2. UPDATE jobs SET status='running', claim_token=?, started_at=NOW
         WHERE id = (SELECT id FROM jobs WHERE status='queued'
                     ORDER BY priority DESC, id ASC LIMIT 1);
      3. SELECT * FROM jobs WHERE claim_token=?  — confirm what we got.
      4. Lease expiry sweeper reclaims jobs whose started_at is older
         than worker.job_lease_seconds and status='running'.
    """

    __tablename__ = "jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)  # e.g. 'index_file'
    payload: Mapped[str] = mapped_column(Text, nullable=False)  # JSON string
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="queued")
    claim_token: Mapped[Optional[str]] = mapped_column(String(36), nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.current_timestamp()
    )
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    __table_args__ = (
        Index("ix_jobs_status_priority_id", "status", "priority", "id"),
        Index("ix_jobs_claim_token", "claim_token"),
    )

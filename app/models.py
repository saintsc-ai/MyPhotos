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
    LargeBinary,
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
    # JSON list of relative paths (POSIX, no leading slash) under
    # abs_path that the scanner skips and the gallery hides. None /
    # empty list means "index everything under this root".
    # Helper accessors in app.scanner.utils.root_ignore_paths.
    ignore_paths: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
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
    file_size: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, index=True)
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

    # When a user manually edits `taken_at`, the EXIF original is snapshotted
    # here so the edit can be reverted later. NULL means `taken_at` is the
    # original EXIF value.
    taken_at_original: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    # Shared description / caption (not author-attributed). Editable by any
    # logged-in user; lives separately from per-user comments.
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # Pipeline status — each stage tracks its own outcome.
    # values: pending | ok | partial | failed | skipped
    exif_status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    exif_extractor: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)
    exif_error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    thumb_status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    thumb_error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # ML classification stage (YOLO / CLIP / face). Independent of the
    # thumb/exif pipeline so partial progress survives a model swap.
    classify_status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="pending"
    )  # pending | ok | failed | skipped

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
    # Per-user permission flags (P1 of access control). Admin ignores
    # these — they only constrain non-admin users. Default FALSE for
    # newly-created accounts; alembic 0012 UPDATEd existing rows to
    # TRUE so behavior didn't change at upgrade time.
    can_upload: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    can_delete: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    can_share: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    can_edit_meta_others: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.current_timestamp()
    )
    last_login_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)


class RootACL(Base):
    """Per-root, per-user access level (P2 of access control).

    Absence of a row = default `read`. A row's `level` is one of
    hidden / read / interact / contribute / manage (see
    docs/ACCESS_CONTROL_PLAN.md §2.2 for what each tier permits).
    Admin bypasses this table entirely.
    """

    __tablename__ = "root_acl"
    __table_args__ = (
        CheckConstraint(
            "level IN ('hidden','read','interact','contribute','manage')",
            name="ck_root_acl_level",
        ),
        Index("ix_root_acl_user", "user_id"),
    )

    root_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("roots.id", ondelete="CASCADE"), primary_key=True,
    )
    user_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True,
    )
    level: Mapped[str] = mapped_column(String(16), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.current_timestamp(),
    )


class Tag(Base):
    """Single normalized tag value (case-sensitive uniqueness on `name`).

    Tag names are deduplicated case-insensitively in the API layer before
    insert, so the user can paste "Pixar" or "pixar" and we settle on one.

    `source` distinguishes user-applied tags from ML-generated ones so the
    UI can render them differently (different chip colour, separate sections
    in the 주제 tab):
      - 'user'      : added in the lightbox tag input
      - 'auto-yolo' : YOLO object detection
      - 'auto-clip' : CLIP zero-shot category match
      - 'face'      : reserved for face cluster labels
    """

    __tablename__ = "tags"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    source: Mapped[str] = mapped_column(String(16), nullable=False, default="user")
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.current_timestamp()
    )


class PhotoTag(Base):
    """User-applied tags only.

    ML-generated labels live in `photo_auto_tags` so they can't be
    clobbered when the user re-saves their tag set (which is a full
    replace of this table for that photo). Both tables share the
    `tags` dictionary so the name "고양이" resolves to one Tag row
    regardless of source.
    """

    __tablename__ = "photo_tags"

    photo_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("photos.id", ondelete="CASCADE"), primary_key=True
    )
    tag_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("tags.id", ondelete="CASCADE"), primary_key=True
    )

    __table_args__ = (
        Index("ix_photo_tags_tag_id", "tag_id"),
    )


class PhotoAutoTag(Base):
    """ML-generated labels, separate from user tags.

    PK includes `source` so the same name (e.g. "person") detected by
    YOLO AND matched by CLIP for the same photo is stored as two rows
    — useful for "show me YOLO's view" vs "CLIP's view" comparison
    later, and lets each ML stage replace just its own rows on
    re-classification.

    confidence is optional — YOLO/CLIP both expose a score and storing
    it lets the UI sort or threshold; face clusters won't fill it.
    """

    __tablename__ = "photo_auto_tags"

    photo_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("photos.id", ondelete="CASCADE"), primary_key=True
    )
    tag_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("tags.id", ondelete="CASCADE"), primary_key=True
    )
    source: Mapped[str] = mapped_column(String(16), primary_key=True)
    confidence: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.current_timestamp()
    )

    __table_args__ = (
        Index("ix_photo_auto_tags_tag_id", "tag_id"),
        Index("ix_photo_auto_tags_source", "source"),
    )


class PhotoEmbedding(Base):
    """One image-level embedding per photo (CLIP). Stored as raw bytes so we
    don't pay JSON overhead — interpret with numpy.frombuffer.

    `vector` is float16, length depends on model (512 for ViT-B/32).
    Keep the model name alongside so future migrations can keep multiple
    embedding spaces if we ever swap models.
    """

    __tablename__ = "photo_embeddings"

    photo_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("photos.id", ondelete="CASCADE"), primary_key=True
    )
    model: Mapped[str] = mapped_column(String(64), nullable=False)
    vector: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
        server_default=func.current_timestamp(),
        onupdate=func.current_timestamp(),
    )


class FaceCluster(Base):
    """User-namable group of face_id rows that we think belong to one person.

    `label` is the user-assigned name (예: '엄마'). NULL while the cluster
    is auto-generated and not yet reviewed. `centroid` is the running mean
    embedding — used to assign new faces to the nearest cluster cheaply.
    """

    __tablename__ = "face_clusters"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    label: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    centroid: Mapped[Optional[bytes]] = mapped_column(LargeBinary, nullable=True)
    face_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.current_timestamp()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
        server_default=func.current_timestamp(),
        onupdate=func.current_timestamp(),
    )


class PhotoFace(Base):
    """One detected face within a photo. Zero or many per photo."""

    __tablename__ = "photo_faces"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    photo_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("photos.id", ondelete="CASCADE"), nullable=False, index=True
    )
    bbox_json: Mapped[str] = mapped_column(Text, nullable=False)  # [x, y, w, h] in [0..1]
    embedding: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    cluster_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("face_clusters.id", ondelete="SET NULL"), nullable=True, index=True
    )
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    indexed_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.current_timestamp()
    )


class PhotoRating(Base):
    """Per-user 1–5 star rating of a photo.

    Composite PK on (photo_id, user_id) — one rating per user per photo.
    Clearing the rating deletes the row; we don't keep a sentinel zero.
    """

    __tablename__ = "photo_ratings"

    photo_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("photos.id", ondelete="CASCADE"), primary_key=True
    )
    user_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    rating: Mapped[int] = mapped_column(Integer, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
        server_default=func.current_timestamp(),
        onupdate=func.current_timestamp(),
    )

    __table_args__ = (
        CheckConstraint("rating BETWEEN 1 AND 5", name="ck_rating_range"),
    )


class PhotoComment(Base):
    """Flat (non-threaded) comments on a photo.

    user_id is set NULL on user delete so the body stays for context but
    the author becomes anonymous in the UI.
    """

    __tablename__ = "photo_comments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    photo_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("photos.id", ondelete="CASCADE"), nullable=False, index=True
    )
    user_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    body: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.current_timestamp()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
        server_default=func.current_timestamp(),
        onupdate=func.current_timestamp(),
    )


class Share(Base):
    """Public, token-addressable view of one or more photos.

    Photos belong to the share via `share_items`. The legacy `photo_id`
    column is still here (nullable) so older rows resolve, but new
    shares always populate share_items even for a single photo.

    Token-only access for passwordless shares — keep tokens long+random.
    `revoked_at` is a kill switch independent of `expires_at`.
    """

    __tablename__ = "shares"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    token: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    # Legacy single-photo column — keep so old shares still resolve. New
    # code reads share_items first.
    photo_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("photos.id", ondelete="CASCADE"), nullable=True
    )
    password_hash: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    title: Mapped[Optional[str]] = mapped_column(String(256), nullable=True)
    expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    view_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # Optional cap on the number of downloads (original-file + ZIP). null
    # means unlimited. download_count covers both file kinds.
    max_downloads: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    download_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_by_user_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("users.id"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.current_timestamp()
    )
    revoked_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)


class ShareItem(Base):
    """One photo's membership in a share. Composite PK (share_id, photo_id)."""

    __tablename__ = "share_items"

    share_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("shares.id", ondelete="CASCADE"),
        primary_key=True,
        index=True,
    )
    photo_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("photos.id", ondelete="CASCADE"),
        primary_key=True,
    )
    sort_idx: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


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

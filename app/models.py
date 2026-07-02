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
    BigInteger,
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
    text,
)
from sqlalchemy.dialects.mysql import VARCHAR as MySQLVARCHAR
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def _path_varchar(length: int = 512):
    """VARCHAR(length) that uses utf8mb4_bin on MySQL / MariaDB.

    SQLite's default text comparison is BINARY (case- and byte-sensitive)
    so 'IMG.mov' and 'IMG.MOV' coexist as distinct rows. MariaDB's
    default collation `utf8mb4_unicode_ci` is CASE-INSENSITIVE — those
    two rows collide on UNIQUE(root_id, rel_path) with
    ERROR 1062 "Duplicate entry" during a SQLite → MariaDB migration.

    Pinning the column to utf8mb4_bin restores SQLite-compatible
    behavior (binary, byte-for-byte comparison) on MariaDB only —
    SQLite and PostgreSQL paths get a plain VARCHAR. PostgreSQL's
    default text comparison is already case-sensitive so no special
    handling needed there.
    """
    return String(length).with_variant(
        MySQLVARCHAR(length, collation="utf8mb4_bin"),
        "mysql", "mariadb",
    )


class Base(DeclarativeBase):
    pass


class Root(Base):
    __tablename__ = "roots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    label: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    abs_path: Mapped[str] = mapped_column(Text, nullable=False)
    readonly: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    # 'photo' (media library — EXIF/thumbnail/ML pipeline) or 'file' (general
    # documents — light index into the `files` table, no media pipeline). The
    # scanner branches on this per root, so photo and document folders stay
    # cleanly separated. Defaults to 'photo' for backward compatibility.
    kind: Mapped[str] = mapped_column(
        String(16), nullable=False, default="photo", server_default=text("'photo'")
    )
    # When true, files indexed under this root that didn't arrive via the
    # authenticated /upload flow (no UploadPending) get their uploader from
    # the first path segment, if it matches a User.username — e.g.
    # ``<root>/<username>/…`` for a PhotoSync-over-SMB drop folder. Opt-in so
    # roots whose top folders are years/albums are never misattributed.
    owner_from_subfolder: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("0")
    )
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
    # _path_varchar(): VARCHAR(512) on every backend, plus utf8mb4_bin
    # collation on MySQL / MariaDB so case-sensitive paths like
    # 'IMG.mov' and 'IMG.MOV' stay distinct (matches SQLite's BINARY
    # behavior and stops ERROR 1062 on migration). Also keeps the
    # composite UNIQUE(root_id, rel_path) under InnoDB's 3072-byte
    # ceiling — 512 chars × 4 utf8mb4 bytes = 2048 bytes.
    rel_path: Mapped[str] = mapped_column(_path_varchar(512), nullable=False)
    filename: Mapped[str] = mapped_column(String(512), nullable=False)
    ext: Mapped[str] = mapped_column(String(16), nullable=False)
    media_kind: Mapped[str] = mapped_column(String(16), nullable=False)  # image | video

    # Identity / change detection
    sha256: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, index=True)
    # BigInteger, not Integer — MariaDB / PostgreSQL map Integer to signed
    # 32-bit (max ~2.1 GB), which overflows on a single multi-GB video.
    # SQLite stores all INTEGER as 64-bit anyway, so this is harmless there.
    # PK columns elsewhere intentionally stay Integer (SQLite ROWID alias).
    file_size: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True, index=True)
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
    # Web-playable H.264 proxy (videos only), built lazily on first failed
    # playback. NULL = playable as-is / never requested.
    # values: pending | running | done | failed
    proxy_status: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)
    proxy_error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # ML classification stage (YOLO / CLIP / face). Independent of the
    # thumb/exif pipeline so partial progress survives a model swap.
    # Rolled-up ML status (ok when objects+clip+faces are all ok/skipped).
    # Kept for back-compat (ML donut / stats / filters); maintained by the
    # unified classify_ml job from the per-stage columns below.
    classify_status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="pending"
    )  # pending | ok | failed | skipped
    # Per-stage ML status — image=key, one column per work item, so stages
    # are requested/skipped/retried/counted independently.
    objects_status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="pending"
    )  # YOLO objects
    clip_status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="pending"
    )  # CLIP embedding + scene tags
    faces_status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="pending"
    )  # YuNet/SFace faces

    # OCR stage (opt-in, like classify). ocr_text feeds the FTS index.
    # ocr_status: NULL=not attempted | pending | ok | empty | failed | skipped
    ocr_text: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    ocr_status: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)

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

    # Per-photo visibility (P4 of access control).
    # 'inherit'  — fall through to folder_acl / root_acl (default)
    # 'private'  — only owner + admin can see, regardless of ACL
    # 'public'   — force at least level=read on top of everything,
    #              re-exposes one photo inside a hidden root
    # owner_user_id is populated by the upload endpoint; legacy rows
    # leave it NULL and become admin-only for private toggles.
    owner_user_id: Mapped[Optional[int]] = mapped_column(
        Integer,
        ForeignKey("users.id", ondelete="SET NULL", name="fk_photos_owner_user_id"),
        nullable=True,
    )
    visibility: Mapped[str] = mapped_column(
        String(16), nullable=False, default="inherit",
    )
    # P5: who sent this photo to trash. Lets the trash list isolate
    # per-user deletions (admin sees everything, family members see
    # only what they trashed). Legacy trashed rows stay NULL.
    trashed_by_user_id: Mapped[Optional[int]] = mapped_column(
        Integer,
        ForeignKey("users.id", ondelete="SET NULL",
                   name="fk_photos_trashed_by_user_id"),
        nullable=True,
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
        Index("ix_photos_objects_status", "objects_status"),
        Index("ix_photos_clip_status", "clip_status"),
        Index("ix_photos_faces_status", "faces_status"),
    )


class PhotoLocation(Base):
    """Separate table for GPS data — kept out of `photos` so most rows stay narrow.

    `source` distinguishes where the coordinates came from:
      'exif'      — read straight off the file (default for rows that
                    pre-date the column).
      'estimated' — inferred by location_estimator from anchor photos
                    in the same/parent folder taken near the same time.
                    `estimated_from_photo_ids` (JSON list, 1–2 ids)
                    names the anchor photos so the lightbox can show
                    "추정 — 'IMG_1234.jpg' 기준" and the user can
                    follow it back.
      'user'     — user typed / picked the coordinates explicitly
                    from the lightbox (P3, not implemented yet —
                    reserved here so we don't have to migrate again).
    """

    __tablename__ = "photo_locations"

    photo_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("photos.id", ondelete="CASCADE"), primary_key=True
    )
    latitude: Mapped[float] = mapped_column(Float, nullable=False)
    longitude: Mapped[float] = mapped_column(Float, nullable=False)
    altitude: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    # NULL means 'exif' for legacy rows; new writes always set it.
    source: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)
    # JSON list of photo ids that the estimate was derived from. NULL
    # for non-estimated rows. Stored as Text so SQLite + MariaDB
    # treatment is identical (no native JSON column dependency).
    estimated_from_photo_ids: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    estimated_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    photo: Mapped[Photo] = relationship(back_populates="location")

    __table_args__ = (
        CheckConstraint("latitude BETWEEN -90 AND 90", name="ck_latitude_range"),
        CheckConstraint("longitude BETWEEN -180 AND 180", name="ck_longitude_range"),
        Index("ix_photo_locations_lat_lon", "latitude", "longitude"),
        Index("ix_photo_locations_source", "source"),
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
    # Human-readable name (실제 이름). `username` is the login ID and is
    # restricted to ASCII (Korean can't be typed into it), so this column
    # holds who the account actually belongs to — e.g. "홍길동". Required;
    # surfaced in the admin UI and usable wherever we'd otherwise show the
    # bare username.
    display_name: Mapped[str] = mapped_column(String(128), nullable=False)
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
    # Account lockout state. Consecutive failed logins increment the count;
    # crossing the configured threshold stamps locked_until in the future.
    # A successful login resets both. (See app.auth.)
    failed_login_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    locked_until: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)


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


class FolderACL(Base):
    """Per-folder, per-user access level (P3 of access control).

    Layered on top of root_acl: when a photo's rel_path matches
    a folder_acl path_prefix (longest match wins), that level
    overrides whatever root_acl says. `path_prefix` is stored with
    a trailing slash for unambiguous LIKE matching — e.g.
    'family/private/' won't accidentally hit 'family/private2/x.jpg'.

    Admin bypasses this table.
    """

    __tablename__ = "folder_acl"
    __table_args__ = (
        CheckConstraint(
            "level IN ('hidden','read','interact','contribute','manage')",
            name="ck_folder_acl_level",
        ),
        Index("ix_folder_acl_user_root", "user_id", "root_id"),
    )

    root_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("roots.id", ondelete="CASCADE"), primary_key=True,
    )
    # VARCHAR(512) rather than TEXT because MariaDB/MySQL refuse TEXT
    # columns in a PRIMARY KEY without an explicit key-length prefix
    # (ERROR 1170 "BLOB/TEXT column used in key specification without
    # a key length"). InnoDB utf8mb4 caps the total composite-PK index
    # at 3072 bytes; 512 chars × 4 bytes = 2048 bytes leaves room for
    # the two INT siblings (root_id + user_id) plus future per-row
    # overhead. 512 chars is still far above any realistic folder
    # path. SQLite ignores the length and treats it as TEXT, so
    # nothing changes there.
    path_prefix: Mapped[str] = mapped_column(String(512), primary_key=True)
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
    # quote=True forces backtick wrapping in the emitted DDL +
    # queries. Required because MariaDB 11.7+ promoted `VECTOR` to a
    # reserved data-type keyword (for the new in-engine vector index
    # feature); without quoting, the parser tries to read `vector
    # BLOB NOT NULL` as "column of type vector BLOB", which fails
    # with a syntax error on `BLOB`. SQLite is unaffected — it
    # treats quoted identifiers identically.
    vector: Mapped[bytes] = mapped_column("vector", LargeBinary, nullable=False, quote=True)
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
    # Provenance: 'detector' = YuNet auto-detection (default); 'user' =
    # admin manually drew the box via POST /api/admin/ml/faces. NULL on
    # rows that pre-date this column — treat as 'detector' at read time.
    # Used by run_detect_faces to keep user-drawn boxes across re-runs,
    # and by the lightbox to render them with a distinct outline.
    source: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)
    indexed_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.current_timestamp()
    )


class PhotoObject(Base):
    """One detected object within a photo. Zero or many per photo.

    Parallels PhotoFace but for YOLO/object-detection output. Stored
    separately from PhotoAutoTag because:
      - PhotoAutoTag is bbox-free, joins through the global tags
        dictionary, and powers the gallery's tag-filter UX.
      - PhotoObject carries the spatial bbox + per-detection
        confidence + source ('detector' | 'user'), and exists per
        detection — three dogs in one photo = three rows.

    Both are populated by the same classify_ml run (since the YOLO
    pass produces both spatial detections and the deduped class set
    for tag chips), so a write should always update both tables in
    the same transaction. See run_classify_objects.
    """

    __tablename__ = "photo_objects"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    photo_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("photos.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # User-visible name. Free-text — admins can rename to whatever they
    # want (e.g. "강아지" instead of "dog"), so this is NOT a foreign key
    # to tags. Kept in sync with PhotoAutoTag.tag_id when the row is
    # created from a YOLO detection, but diverges as soon as the admin
    # renames the object. Indexed for "all photos containing X" search.
    label: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    bbox_json: Mapped[str] = mapped_column(Text, nullable=False)  # [x, y, w, h] in [0..1]
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    # 'detector' = YOLO auto-detection (default); 'user' = admin
    # manually drew the box via POST /api/admin/ml/photos/{id}/objects.
    # Same survival semantics as PhotoFace.source: user-drawn rows
    # outlive re-detection runs; detector rows get replaced.
    source: Mapped[str] = mapped_column(String(16), nullable=False, default="detector")
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
    # When true, the public original-file download strips GPS + other
    # identifying EXIF tags before streaming. Default false preserves
    # behaviour for shares created before this column existed.
    strip_exif: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
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


class ShareFileItem(Base):
    """A file's membership in a share. Mirror of ShareItem for the files
    domain — keeps photo shares (share_items) completely untouched.
    Composite PK (share_id, file_id)."""

    __tablename__ = "share_file_items"

    share_id: Mapped[int] = mapped_column(
        ForeignKey("shares.id", ondelete="CASCADE"), primary_key=True, index=True
    )
    file_id: Mapped[int] = mapped_column(
        ForeignKey("files.id", ondelete="CASCADE"), primary_key=True
    )
    sort_idx: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class File(Base):
    """A non-media file under a ``kind='file'`` root — sibling to Photo.

    Reuses roots / ACL / shares / scan but skips the media pipeline
    (EXIF / thumbnail / ML). The scanner indexes name + path + hash + mime
    on the fast path so a file is searchable by name immediately; document
    *content* text is extracted asynchronously into `files_fts`, tracked by
    ``text_status`` (pending / ok / none / failed) the same way photos track
    their per-stage status.
    """

    __tablename__ = "files"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    root_id: Mapped[int] = mapped_column(
        ForeignKey("roots.id", ondelete="CASCADE"), nullable=False, index=True
    )
    rel_path: Mapped[str] = mapped_column(_path_varchar(512), nullable=False)
    filename: Mapped[str] = mapped_column(String(512), nullable=False)
    ext: Mapped[str] = mapped_column(String(32), nullable=False)
    # Containing folder (POSIX rel path, '' for root-level). Denormalised from
    # rel_path so the explorer lists a folder by indexed equality/DISTINCT
    # instead of a leading-wildcard LIKE scan over the whole subtree.
    parent: Mapped[str] = mapped_column(
        _path_varchar(512), nullable=False, server_default=""
    )
    sha256: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, index=True)
    mime: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    file_size: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True, index=True)
    mtime: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    # Cheap signature for incremental scan: usually f"{size}:{mtime_ns}".
    content_signature: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="active", server_default=text("'active'")
    )
    # Content-text extraction: NULL/pending = not done yet, ok = text in
    # files_fts, none = format unsupported / no extractor installed, failed.
    text_status: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)
    text_engine: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    owner_user_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("users.id"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.current_timestamp()
    )

    __table_args__ = (
        UniqueConstraint("root_id", "rel_path", name="uq_files_root_relpath"),
        Index("ix_files_status", "status"),
        Index("ix_files_text_status", "text_status"),
        Index("ix_files_root_parent", "root_id", "parent"),
    )


class FileText(Base):
    """Extracted document text for a File — kept out of the files row (which
    is loaded on every listing) so those queries stay lean. Composed into
    file_fts for content search. One row per file, created only once text
    extraction succeeds."""

    __tablename__ = "file_text"

    file_id: Mapped[int] = mapped_column(
        ForeignKey("files.id", ondelete="CASCADE"), primary_key=True
    )
    body: Mapped[str] = mapped_column(Text, nullable=False)


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
    # Per-photo jobs (classify_ml, ocr_text, …) also store the photo_id
    # in this dedicated column so enqueue_unique_for_photo() can SELECT
    # without parsing JSON. NULL for non-photo jobs (discover_root etc.)
    # — `ix_jobs_kind_photo_status` is partial-friendly: most DBs skip
    # NULL entries in a composite index, so those jobs stay cheap.
    photo_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
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
    # Generic progress counters for long-running jobs. total=0 means the
    # job hasn't computed its size yet; done is incremented as work lands.
    progress_done: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    progress_total: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    __table_args__ = (
        Index("ix_jobs_status_priority_id", "status", "priority", "id"),
        Index("ix_jobs_claim_token", "claim_token"),
        # Composite for the per-photo dedup lookup
        #   SELECT id FROM jobs WHERE kind=? AND photo_id=? AND status IN (...)
        # Column order matches the SELECT shape so the planner can use a
        # range scan even when status is filtered with IN.
        Index("ix_jobs_kind_photo_status", "kind", "photo_id", "status"),
    )


class PhotoWork(Base):
    """Photo-unit work queue (alternative to per-stage jobs).

    The legacy `jobs` table treats every pipeline stage (index, classify,
    estimate_location, transcode_proxy, …) as its own row, which means
    a single photo can sit in the queue under 4-5 different `kind`
    rows. That made dedup awkward (per-kind dedup, not per-photo),
    inflated the queue, and forced the worker to grab the same SQLite
    write lock 4-5 times to finish one photo.

    `photo_work` flips the model: one row per photo, with a JSON
    `stages` map (`{"index": "pending", "classify": "ok", ...}`) that
    the dispatcher walks through in fixed order. Requesting a new
    stage is an UPDATE on an existing row — no extra INSERT, no
    chance of two competing classify_ml rows for the same photo.

    Lifecycle:
      - photo discovered → INSERT row with stages={"index": "pending"}
      - new stage requested (admin click, lightbox 📍 button, …) →
        UPDATE stages[name] = "pending" (or queue-up via API helper)
      - worker claims one row at a time (claim_token), walks pending
        stages in STAGE_ORDER, commits per stage, cooperative on
        _stop between stages
      - all stages settled (ok / failed / skipped) → row deleted (or
        kept for history if needed later)

    Coexists with the legacy `jobs` table during migration. The two
    dispatchers run side by side until callers are flipped over and
    the old kinds drained.
    """

    __tablename__ = "photo_work"

    photo_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("photos.id", ondelete="CASCADE"), primary_key=True
    )
    # JSON object: {stage_name: "pending"|"ok"|"failed"|"skipped", ...}
    # Stored as Text so SQLite + MariaDB treat it identically (no JSON
    # column dep). Worker reads + writes with json.loads / json.dumps.
    stages: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    # JSON object: {stage_name: {param: value, ...}}. Carries trigger-
    # supplied params (e.g. estimate_location threshold) to the handler.
    # Defaults to "{}" so existing rows from before this column was
    # added don't trip the JSON parse.
    stage_params: Mapped[str] = mapped_column(
        Text, nullable=False, default="{}", server_default="{}",
    )
    # Higher = sooner. Default mirrors the legacy jobs.priority semantics
    # (the dispatcher claim ORDER BY priority DESC, id ASC).
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    claim_token: Mapped[Optional[str]] = mapped_column(String(36), nullable=True)
    claimed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.current_timestamp()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
        server_default=func.current_timestamp(),
        onupdate=func.current_timestamp(),
    )

    __table_args__ = (
        # Dispatcher claim — unclaimed rows in priority order.
        Index("ix_photo_work_claim", "claim_token", "priority", "photo_id"),
        # Stale-claim sweeper finds rows whose worker died holding the
        # token. Cheap: only running rows have a non-null claimed_at.
        Index("ix_photo_work_claimed_at", "claimed_at"),
    )


class AuditLog(Base):
    """Append-only record of who did what when (P5).

    Captures every privileged action so the admin can reconstruct
    "who deleted this", "when was the ACL changed", etc. Username is
    denormalised so the row keeps meaning after the user is deleted.

    `resource_id` is a string (not int) so we can write composite ids
    like 'root_id=1:path=family/private/' for folder ACL changes.
    `detail` is freeform JSON when before/after context helps.
    """

    __tablename__ = "audit_log"
    __table_args__ = (
        Index("ix_audit_log_ts", "ts"),
        Index("ix_audit_log_user_ts", "user_id", "ts"),
        Index("ix_audit_log_resource", "resource_type", "resource_id", "ts"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.current_timestamp(),
    )
    user_id: Mapped[Optional[int]] = mapped_column(
        Integer,
        ForeignKey("users.id", ondelete="SET NULL",
                   name="fk_audit_log_user_id"),
        nullable=True,
    )
    username: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    resource_type: Mapped[str] = mapped_column(String(32), nullable=False)
    resource_id: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    detail: Mapped[Optional[str]] = mapped_column(Text, nullable=True)


class UploadPending(Base):
    """Maps a freshly uploaded file to the user who uploaded it, so
    the indexer can stamp Photo.owner_user_id when the matching Photo
    row is created.

    Decoupled from Photo because the upload endpoint only drops bytes
    on disk — the Photo row is created later by index_file when the
    scanner notices the file. Same path can be re-uploaded; the unique
    constraint forces last-writer-wins on the (root_id, rel_path,
    filename) triple.

    A worker tick (see app/worker/main.py) drops rows older than 7 days
    so failed uploads / non-indexable files don't accumulate.
    """

    __tablename__ = "uploads_pending"
    __table_args__ = (
        UniqueConstraint(
            "root_id", "rel_path",
            name="uq_uploads_pending_path",
        ),
        Index("ix_uploads_pending_created_at", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    root_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("roots.id", ondelete="CASCADE",
                   name="fk_uploads_pending_root_id"),
        nullable=False,
    )
    # Full POSIX path including filename, matching Photo.rel_path.
    # _path_varchar(): see Photo.rel_path for the binary-collation
    # rationale. UNIQUE(root_id, rel_path) here too, so the same
    # case-sensitivity + key-length constraints apply on MariaDB.
    rel_path: Mapped[str] = mapped_column(_path_varchar(512), nullable=False)
    user_id: Mapped[Optional[int]] = mapped_column(
        Integer,
        ForeignKey("users.id", ondelete="SET NULL",
                   name="fk_uploads_pending_user_id"),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.current_timestamp(),
    )

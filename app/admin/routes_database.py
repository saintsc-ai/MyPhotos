"""Admin endpoints for the DB backend — info, backup, validation.

What's intentionally NOT here:
  - in-place backend switching (config file write + service restart
    require shell access)
  - actual migration execution (use scripts/migrate-db.py from the
    shell so a missing PRAGMA or schema mismatch can't take down the
    live system through a single API call)

What IS here:
  - read-only status (current backend, dsn masked, row counts, file
    size) — useful when porting between hosts
  - on-demand snapshot (sqlite3.backup() for SQLite, mysqldump for
    MariaDB)
  - download of an already-taken snapshot
  - "does this DSN actually accept connections?" probe before the
    admin edits config/local.toml
  - dry-run row-count comparison so admins can see how big the
    migration would be before running scripts/migrate-db.py
"""

from __future__ import annotations

import gzip
import os
import re
import shutil
import sqlite3
import subprocess
from datetime import datetime
from pathlib import Path
from urllib.parse import unquote, urlsplit

from fastapi import APIRouter, Body, Depends, HTTPException, status
from fastapi.responses import FileResponse
from pydantic import BaseModel
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.orm import Session

from ..api.deps import get_db
from ..config import get_settings
from ..db import is_sqlite_url, resolve_db_url
from ..models import Base
from ..paths import DATA_DIR, DB_PATH, PROJECT_ROOT

router = APIRouter(prefix="/admin/database", tags=["admin", "database"])

BACKUPS_DIR = DATA_DIR / "backups"


# ---------- helpers ----------

def _mask_dsn(url: str) -> str:
    """Hide the password segment of a DSN for display."""
    if not url:
        return ""
    if "://" not in url:
        return url
    scheme, rest = url.split("://", 1)
    if "@" not in rest:
        return url
    creds, host_db = rest.split("@", 1)
    if ":" in creds:
        user = creds.split(":", 1)[0]
        return f"{scheme}://{user}:***@{host_db}"
    return url


def _parse_mariadb_dsn(url: str) -> dict[str, str | int]:
    """Pull host / port / user / password / database out of a SQLAlchemy
    mysql+pymysql:// DSN. Used by the mysqldump subprocess.

    urlsplit() returns userinfo / hostname / path components as raw
    URL substrings — percent-encoded characters are NOT decoded. So a
    password like `Foo@Bar` (DSN form `Foo%40Bar`) comes back as the
    literal `Foo%40Bar` from sp.password. SQLAlchemy + pymysql decode
    internally, which is why app DB access works, but mysqldump only
    sees what we hand it via MYSQL_PWD — feeding it `Foo%40Bar` makes
    the server reply with 1045 Access denied. Run unquote() on every
    component that can legitimately contain percent-encoded bytes.
    """
    sp = urlsplit(url)
    return {
        "host": sp.hostname or "localhost",   # sp.hostname is already decoded
        "port": sp.port or 3306,
        "user": unquote(sp.username) if sp.username else "",
        "password": unquote(sp.password) if sp.password else "",
        "database": unquote((sp.path or "").lstrip("/").split("?", 1)[0]),
    }


def _table_counts(eng) -> dict[str, int]:
    out: dict[str, int] = {}
    with eng.connect() as conn:
        for t in Base.metadata.sorted_tables:
            try:
                out[t.name] = conn.execute(
                    select(func.count()).select_from(t)
                ).scalar_one()
            except Exception:
                # Table might not exist on a partly-migrated target.
                out[t.name] = -1
    return out


# ---------- models ----------

class DBInfo(BaseModel):
    backend: str                 # "sqlite" | "mariadb" | "mysql" | ...
    dsn_masked: str
    sqlite_path: str | None = None
    sqlite_size_bytes: int | None = None
    photo_count: int
    table_row_counts: dict[str, int]


class BackupEntry(BaseModel):
    filename: str
    kind: str                    # "sqlite" | "mariadb"
    size_bytes: int
    created_at: datetime


class BackupRequest(BaseModel):
    kind: str = "auto"           # "auto" picks based on active backend


class BackupResult(BaseModel):
    ok: bool
    filename: str | None = None
    size_bytes: int | None = None
    note: str | None = None


class ConnectionTestIn(BaseModel):
    url: str


class CompatIssue(BaseModel):
    """One SQLite-only feature the migration would lose / break when
    moving to a non-SQLite backend. `level` = 'blocker' (won't work at
    all) | 'feature_loss' (works but the feature degrades) | 'minor'
    (small dialect difference, easy to fix).
    """
    code: str
    level: str
    summary: str
    detail: str


class ConnectionTestResult(BaseModel):
    ok: bool
    error: str | None = None
    server_version: str | None = None
    # Same compat-issue list the dry-run returns, populated when the
    # candidate DSN's dialect differs from the running backend. Lets
    # the admin see FTS5 / strftime / GROUP_CONCAT warnings the moment
    # they validate the DSN — they don't have to advance to dry-run
    # before realising the move would degrade features.
    compat_issues: list[CompatIssue] = []


class DryRunIn(BaseModel):
    dst_url: str


class DryRunTable(BaseModel):
    table: str
    src_rows: int
    dst_exists: bool
    dst_rows: int                # 0 when table doesn't exist


class DryRunResult(BaseModel):
    src_dsn_masked: str
    dst_dsn_masked: str
    src_backend: str
    dst_backend: str
    total_src_rows: int
    dst_has_existing_data: bool
    tables: list[DryRunTable]
    note: str
    # Empty when src ↔ dst are the same dialect (no cross-dialect move).
    # When migrating SQLite → MariaDB, surfaces the known SQLite-only
    # features (FTS5, strftime, GROUP_CONCAT sep) so the admin knows
    # what to expect before kicking off the copy.
    compat_issues: list[CompatIssue] = []


# ---------- routes ----------

@router.get("/info", response_model=DBInfo)
def db_info(db: Session = Depends(get_db)) -> DBInfo:
    url = resolve_db_url()
    masked = _mask_dsn(url)
    is_sql = is_sqlite_url(url)
    # Backend name = SQLAlchemy dialect name ("sqlite" / "mysql" /
    # "postgresql"). Beats parsing the URL scheme — survives URLs
    # like `postgresql+psycopg://` or `mariadb+mariadbconnector://`
    # without growing more replace() calls per driver.
    from ..db import engine as _eng
    backend = "sqlite" if is_sql else _eng.dialect.name
    sqlite_size = None
    sqlite_path: str | None = None
    if is_sql:
        sqlite_path = str(DB_PATH)
        try:
            sqlite_size = DB_PATH.stat().st_size
        except OSError:
            sqlite_size = None

    # Total active photo count is the headline metric. Row counts per
    # table go below for the curious.
    from ..models import Photo
    photo_count = db.execute(
        select(func.count()).where(Photo.status == "active")
    ).scalar_one()

    from ..db import engine
    return DBInfo(
        backend=backend,
        dsn_masked=masked,
        sqlite_path=sqlite_path,
        sqlite_size_bytes=sqlite_size,
        photo_count=int(photo_count or 0),
        table_row_counts=_table_counts(engine),
    )


@router.get("/backups", response_model=list[BackupEntry])
def list_backups() -> list[BackupEntry]:
    if not BACKUPS_DIR.exists():
        return []
    out: list[BackupEntry] = []
    for p in sorted(BACKUPS_DIR.iterdir(), key=lambda x: x.stat().st_mtime, reverse=True):
        if not p.is_file():
            continue
        name = p.name
        if name.endswith(".db"):
            kind = "sqlite"
        elif name.endswith(".sql.gz") or name.endswith(".sql"):
            kind = "mariadb"
        else:
            continue
        st = p.stat()
        out.append(BackupEntry(
            filename=name,
            kind=kind,
            size_bytes=st.st_size,
            created_at=datetime.fromtimestamp(st.st_mtime),
        ))
    return out


@router.post("/backup", response_model=BackupResult)
def trigger_backup(body: BackupRequest = Body(default=BackupRequest())) -> BackupResult:
    """Take a fresh snapshot of the active DB.

    SQLite: uses sqlite3 Connection.backup() so we get a consistent
    image even while the worker is writing (works because WAL).

    MariaDB: shells out to mysqldump --single-transaction.
    """
    BACKUPS_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    url = resolve_db_url()
    kind = body.kind
    if kind == "auto":
        kind = "sqlite" if is_sqlite_url(url) else "mariadb"

    if kind == "sqlite":
        if not is_sqlite_url(url):
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                "Active backend is not SQLite — pass kind='mariadb' instead.",
            )
        out = BACKUPS_DIR / f"catalog-{ts}.db"
        try:
            src = sqlite3.connect(str(DB_PATH))
            try:
                dst = sqlite3.connect(str(out))
                try:
                    src.backup(dst)
                finally:
                    dst.close()
            finally:
                src.close()
        except sqlite3.Error as e:
            raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, str(e))
        size = out.stat().st_size
        return BackupResult(ok=True, filename=out.name, size_bytes=size)

    if kind == "mariadb":
        if is_sqlite_url(url):
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                "Active backend is SQLite — pass kind='sqlite' instead.",
            )
        cfg = _parse_mariadb_dsn(url)
        # Hardened DSN component validation — these values land on the
        # mysqldump argv. They originate from config/local.toml which
        # is writable through the admin /settings endpoint, so a
        # compromised admin (or even a write-via-config bug) cannot
        # smuggle shell metacharacters / argv injection through here.
        for key, pat in (
            ("host",     r"^[A-Za-z0-9._:\-]+$"),
            ("user",     r"^[A-Za-z0-9._\-]+$"),
            ("database", r"^[A-Za-z0-9_\-]+$"),
        ):
            val = str(cfg.get(key, ""))
            if not re.fullmatch(pat, val):
                raise HTTPException(
                    status.HTTP_400_BAD_REQUEST,
                    f"DSN의 {key} 값에 허용되지 않은 문자가 있습니다: {val!r}",
                )
        try:
            port = int(cfg["port"])
        except (TypeError, ValueError):
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "DSN의 포트가 유효하지 않습니다")
        out = BACKUPS_DIR / f"catalog-{ts}.sql.gz"
        # Run mysqldump as a list-argv subprocess (no shell), stream
        # stdout into Python's gzip writer. Eliminates the previous
        # shell=True + f-string interpolation that allowed argv /
        # shell-metacharacter injection from the DSN.
        argv = [
            "mysqldump",
            f"--host={cfg['host']}",
            f"--port={port}",
            f"--user={cfg['user']}",
            "--single-transaction",
            "--quick",
            "--default-character-set=utf8mb4",
            str(cfg["database"]),
        ]
        env = {**os.environ, "MYSQL_PWD": str(cfg["password"])}
        try:
            with gzip.open(out, "wb") as gz:
                proc = subprocess.Popen(
                    argv,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    env=env,
                )
                try:
                    # Stream in chunks rather than .communicate() so a
                    # large dump doesn't load into memory.
                    assert proc.stdout is not None
                    while True:
                        chunk = proc.stdout.read(64 * 1024)
                        if not chunk:
                            break
                        gz.write(chunk)
                    rc = proc.wait(timeout=600)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    raise HTTPException(
                        status.HTTP_500_INTERNAL_SERVER_ERROR,
                        "mysqldump 타임아웃 (10분)",
                    )
            if rc != 0:
                err = (proc.stderr.read().decode("utf-8", "replace")
                       if proc.stderr else "")[:500]
                raise HTTPException(
                    status.HTTP_500_INTERNAL_SERVER_ERROR,
                    f"mysqldump 실패 (exit {rc}): {err}",
                )
        except FileNotFoundError:
            raise HTTPException(
                status.HTTP_500_INTERNAL_SERVER_ERROR,
                "mysqldump CLI를 찾지 못했습니다 — 호스트에 설치 필요",
            )
        size = out.stat().st_size
        return BackupResult(ok=True, filename=out.name, size_bytes=size)

    raise HTTPException(status.HTTP_400_BAD_REQUEST, f"unknown kind={kind!r}")


_SAFE_NAME = re.compile(r"^catalog-\d{8}-\d{6}\.(db|sql\.gz|sql)$")


@router.get("/backups/{filename}")
def download_backup(filename: str) -> FileResponse:
    # Guard against path traversal — only accept the timestamp pattern
    # our backup writer produces.
    if not _SAFE_NAME.match(filename):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "잘못된 백업 파일명")
    target = (BACKUPS_DIR / filename).resolve()
    try:
        target.relative_to(BACKUPS_DIR.resolve())
    except ValueError:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "잘못된 경로")
    if not target.exists():
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    return FileResponse(
        target,
        filename=filename,
        media_type="application/octet-stream",
    )


@router.delete("/backups/{filename}")
def delete_backup(filename: str) -> dict[str, str]:
    """Remove a single backup file. Same name-pattern guard as the
    download endpoint so callers can't reach outside BACKUPS_DIR.

    Mostly used to prune zero-byte / partial-write backups left
    behind when mysqldump fails mid-run (the failed cases the user
    saw in the UI), without having to SSH into the host to rm them.
    """
    if not _SAFE_NAME.match(filename):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "잘못된 백업 파일명")
    target = (BACKUPS_DIR / filename).resolve()
    try:
        target.relative_to(BACKUPS_DIR.resolve())
    except ValueError:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "잘못된 경로")
    if not target.exists():
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    try:
        target.unlink()
    except OSError as e:
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            f"삭제 실패: {e}",
        )
    return {"ok": "true", "filename": filename}


@router.post("/test-connection", response_model=ConnectionTestResult)
def test_connection(body: ConnectionTestIn) -> ConnectionTestResult:
    """Probe a candidate DSN. Always returns 200 — the result body
    carries success/failure so the UI can render it without try/catch.

    Also surfaces compat_issues when the candidate dialect differs
    from the currently-running backend, so the admin sees what would
    break/degrade BEFORE running the dry-run."""
    url = body.url.strip()
    if not url:
        return ConnectionTestResult(ok=False, error="URL이 비어있습니다")
    src_dialect = _dialect_of(resolve_db_url())
    dst_dialect = _dialect_of(url)
    issues = _compat_issues_for_move(src_dialect, dst_dialect)
    try:
        eng = create_engine(url, future=True)
        with eng.connect() as conn:
            ver_row = None
            if is_sqlite_url(url):
                ver_row = conn.execute(text("SELECT sqlite_version()")).scalar_one()
            else:
                ver_row = conn.execute(text("SELECT VERSION()")).scalar_one()
        return ConnectionTestResult(
            ok=True, server_version=str(ver_row), compat_issues=issues,
        )
    except Exception as e:
        return ConnectionTestResult(
            ok=False, error=str(e)[:500], compat_issues=issues,
        )


def _dialect_of(url: str) -> str:
    """Pull the dialect name out of a SQLAlchemy URL ('sqlite',
    'mysql', 'postgresql', ...) without instantiating an engine."""
    head = url.split("://", 1)[0].lower()
    # mysql+pymysql → mysql; sqlite → sqlite
    return head.split("+", 1)[0]


def _compat_issues_for_move(src_dialect: str, dst_dialect: str) -> list[CompatIssue]:
    """Static catalogue of SQLite-only features used in this codebase
    that would break / degrade when moving to a non-SQLite backend.

    Updated whenever a new SQLite-isms lands in the code (currently:
    FTS5 virtual table + trigram tokenizer, strftime for the year
    histogram, GROUP_CONCAT separator syntax in fts.py). Surface them
    in the dry-run UI so the admin knows what to expect before
    kicking off a migration that can't be rolled back partway.
    """
    if src_dialect == dst_dialect:
        return []
    if src_dialect != "sqlite":
        # Only SQLite → other-backend is checked; other directions
        # aren't on the supported migration path.
        return []

    return [
        CompatIssue(
            code="fts5",
            level="minor",
            summary="텍스트 검색 — 외부 DB 에선 LIKE 폴백 (느려짐)",
            detail=(
                "alembic/versions/0020_photo_fts.py 와 app/fts.py 의 "
                "FTS5 가상 테이블 + trigram tokenizer 는 SQLite 전용. "
                "`fts.is_available()` 가 non-SQLite dialect 에서 False 로 "
                "단락되고, routes_photos.py 의 검색 헬퍼가 LIKE-OR 폴백 "
                "(filename + rel_path + description + 댓글 + 태그 + "
                "자동태그 + 업로더명 7개 필드 동일) 으로 자동 분기. "
                "결과 정확도는 동일, 응답 시간만 10만 행 기준 "
                "50ms → 1–5초로 늘어남. 사용자 빈도가 낮아 별도 인덱싱 "
                "(trigram extension, pg_trgm 등) 도입은 보류."
            ),
        ),
        CompatIssue(
            code="group_concat_sep",
            level="minor",
            summary="GROUP_CONCAT separator 문법 (현재 도달 불가)",
            detail=(
                "app/fts.py 백필 SQL 의 GROUP_CONCAT(col, ' ') 는 SQLite "
                "문법 — MariaDB 는 GROUP_CONCAT(col SEPARATOR ' '), "
                "PostgreSQL 은 string_agg(col, ' '). 현재 fts5 단락 때문에 "
                "외부 DB 에선 도달 불가. FTS 인덱스를 외부 DB 에서 "
                "재구성하기로 결정하면 그때 함께 처리."
            ),
        ),
    ]


@router.post("/migrate-dry-run", response_model=DryRunResult)
def migrate_dry_run(body: DryRunIn) -> DryRunResult:
    """Compare source (active) vs destination row counts without writing
    anything. Helps the admin see how big the actual migration will be
    and whether the destination already has data."""
    src_url = resolve_db_url()
    dst_url = body.dst_url.strip()
    if not dst_url:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "대상 URL이 비어있습니다")
    if src_url == dst_url:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "원본과 대상 DSN이 같습니다",
        )
    try:
        src_eng = create_engine(src_url, future=True)
        dst_eng = create_engine(dst_url, future=True)
        with src_eng.connect() as c:
            c.execute(text("SELECT 1"))
        with dst_eng.connect() as c:
            c.execute(text("SELECT 1"))
    except Exception as e:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, f"연결 실패: {str(e)[:300]}",
        )

    src_counts = _table_counts(src_eng)
    dst_counts = _table_counts(dst_eng)
    tables: list[DryRunTable] = []
    total_src = 0
    has_existing = False
    for t in Base.metadata.sorted_tables:
        sn = src_counts.get(t.name, -1)
        dn = dst_counts.get(t.name, -1)
        total_src += max(sn, 0)
        if dn > 0:
            has_existing = True
        tables.append(DryRunTable(
            table=t.name,
            src_rows=sn,
            dst_exists=(dn >= 0),
            dst_rows=max(dn, 0),
        ))

    if has_existing:
        note = (
            "⚠ 대상 DB에 이미 데이터가 있습니다. "
            "실제 마이그레이션 시 scripts/migrate-db.py --drop 필수 "
            "(대상 테이블 모두 삭제 후 복사)"
        )
    elif any(not t.dst_exists for t in tables):
        note = (
            "대상에 스키마가 없습니다. migrate-db.py가 자동으로 "
            "테이블을 생성합니다."
        )
    else:
        note = "대상이 비어있고 스키마는 준비됨 — 안전하게 복사 가능."

    src_dialect = _dialect_of(src_url)
    dst_dialect = _dialect_of(dst_url)
    return DryRunResult(
        src_dsn_masked=_mask_dsn(src_url),
        dst_dsn_masked=_mask_dsn(dst_url),
        src_backend=src_dialect,
        dst_backend=dst_dialect,
        total_src_rows=total_src,
        dst_has_existing_data=has_existing,
        tables=tables,
        note=note,
        compat_issues=_compat_issues_for_move(src_dialect, dst_dialect),
    )

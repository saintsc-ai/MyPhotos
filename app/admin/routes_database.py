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
from urllib.parse import urlsplit

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
    mysql+pymysql:// DSN. Used by the mysqldump subprocess."""
    sp = urlsplit(url)
    return {
        "host": sp.hostname or "localhost",
        "port": sp.port or 3306,
        "user": sp.username or "",
        "password": sp.password or "",
        "database": (sp.path or "").lstrip("/").split("?", 1)[0],
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


class ConnectionTestResult(BaseModel):
    ok: bool
    error: str | None = None
    server_version: str | None = None


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
    total_src_rows: int
    dst_has_existing_data: bool
    tables: list[DryRunTable]
    note: str


# ---------- routes ----------

@router.get("/info", response_model=DBInfo)
def db_info(db: Session = Depends(get_db)) -> DBInfo:
    url = resolve_db_url()
    masked = _mask_dsn(url)
    is_sql = is_sqlite_url(url)
    backend = "sqlite" if is_sql else (url.split(":", 1)[0].replace("+pymysql", "") or "unknown")
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


@router.post("/test-connection", response_model=ConnectionTestResult)
def test_connection(body: ConnectionTestIn) -> ConnectionTestResult:
    """Probe a candidate DSN. Always returns 200 — the result body
    carries success/failure so the UI can render it without try/catch."""
    url = body.url.strip()
    if not url:
        return ConnectionTestResult(ok=False, error="URL이 비어있습니다")
    try:
        eng = create_engine(url, future=True)
        with eng.connect() as conn:
            ver_row = None
            if is_sqlite_url(url):
                ver_row = conn.execute(text("SELECT sqlite_version()")).scalar_one()
            else:
                ver_row = conn.execute(text("SELECT VERSION()")).scalar_one()
        return ConnectionTestResult(ok=True, server_version=str(ver_row))
    except Exception as e:
        return ConnectionTestResult(ok=False, error=str(e)[:500])


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

    return DryRunResult(
        src_dsn_masked=_mask_dsn(src_url),
        dst_dsn_masked=_mask_dsn(dst_url),
        total_src_rows=total_src,
        dst_has_existing_data=has_existing,
        tables=tables,
        note=note,
    )

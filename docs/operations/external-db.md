# 외부 DB (MariaDB / PostgreSQL) 사용

> 한국어 / [English](#english)

> [← README로 돌아가기](../../README.md)

기본은 `data/catalog.db` (SQLite, 단일 파일)이며 대부분의 가족 단위
운영에는 충분합니다. 같은 NAS에 이미 돌고 있는 MariaDB(또는 MySQL) /
PostgreSQL 인스턴스를 카탈로그용으로 같이 쓰고 싶을 때 DSN을 설정해서
백엔드를 바꿀 수 있습니다.

> **테스트 상태**: SQLite 와 MariaDB는 실제 풀-마이그레이션(SQLite →
> MariaDB, 약 7만 장 catalog)으로 검증됨. PostgreSQL은 SQLAlchemy /
> Alembic 호환성 + dialect-분기 코드로 구현은 갖췄으나 작성자가 보유한
> 인스턴스에서 풀스택을 직접 검증하지는 않았습니다 — 새 PG 환경에서
> 처음 마이그레이션할 때는 작은 카탈로그로 dry-run을 권합니다.

명령은 두 종류를 함께 보여줍니다:

- **Linux / Synology** (systemd 기반) — `sudo systemctl ...`
- **Windows** (개발용 PowerShell) — `.\scripts\myphotos.ps1 ...`

## 외부 DB에서 기능 차이 (꼭 먼저 읽기)

코드가 SQLite를 1차 백엔드로 만들면서 일부 SQLite 전용 기능을 직접
사용합니다. 외부 DB로 옮길 때 다음과 같이 동작이 바뀝니다 — MariaDB / PG
모두 해당:

| 기능 | 외부 DB에서의 동작 |
| --- | --- |
| 사진 목록 / 필터 / 정렬 | **정상**. ORDER BY의 `NULLS LAST`는 dialect-분기 compiler로 처리 (MariaDB가 native 지원 안 함). |
| 연도별 타임라인 스크롤 | **정상**. SQLAlchemy `extract("year", ...)` 로 dialect-portable. |
| 지도 / 클러스터 / GPS / 카메라 / 별점 / 댓글 필터 | **정상**. 일반 SQL. |
| 썸네일 / 잡 큐 / 워커 | **정상**. 잡 큐 claim 패턴은 `UPDATE ... WHERE id = (SELECT ... LIMIT 1)` — 모든 백엔드 동일. |
| **텍스트 검색 (검색바)** | **빈 결과 반환**. FTS5 가상 테이블 + trigram tokenizer는 SQLite 전용 — `fts.is_available()` 가 non-SQLite에서 False로 단락되어 검색이 사실상 비활성화됨. 다른 필터(날짜/별점/카메라 등)는 정상이라 그것들로 좁히면 됨. LIKE-OR 폴백 구현은 별도 TODO. |

이 차이는 관리 → DB 페이지의 마이그레이션 dry-run 에서도 표시됩니다.

> **마이그레이션 도중 만났던 문제들 (이미 코드에 반영됨)**
>
> SQLite → MariaDB 첫 풀-마이그레이션에서 다음 4가지 SQLite vs MariaDB
> 차이가 순서대로 터졌고 alembic 0023–0026 + 런타임 fix 로 모두 해결된
> 상태입니다. 새 마이그레이션은 처음부터 깨끗하게 통과합니다:
>
> | 에러 | 원인 | 해결 |
> | --- | --- | --- |
> | `1170` (`BLOB/TEXT column used in key spec`) | TEXT 컬럼이 UNIQUE 인덱스에 들어감 (`folder_acl.path_prefix`, `photos.rel_path`, `uploads_pending.rel_path`) | 0023, 0024: VARCHAR(512) 로 ALTER |
> | `1264` (`Out of range value for file_size`) | SQLAlchemy `Integer` → MariaDB `INT(11)` (32-bit signed, max ~2GB) → 멀티-GB 영상에서 오버플로 | 0025: `BigInteger` (BIGINT 64-bit). PG에도 동일하게 필요 |
> | `1062` (`Duplicate entry for rel_path`) | SQLite 기본 비교는 BINARY (대소문자 구분), MariaDB 기본 `utf8mb4_unicode_ci` 는 case-insensitive → `IMG.mov` 와 `IMG.MOV` 가 충돌 | 0026: `rel_path` 컬럼을 `utf8mb4_bin` 으로 ALTER. PG는 기본이 이미 case-sensitive |
> | `1146` (`Table 'sqlite_master' doesn't exist`) | `fts.is_available()` 가 sqlite_master 직접 조회 | dialect 단락 — non-SQLite는 False 즉시 반환 |

## 0) DB와 사용자 준비

### MariaDB / MySQL

```sql
CREATE DATABASE myphotos
    CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
CREATE USER 'myphotos'@'%' IDENTIFIED BY '강한_비밀번호';
GRANT ALL PRIVILEGES ON myphotos.* TO 'myphotos'@'%';
FLUSH PRIVILEGES;
```

`localhost` 로만 접속한다면 `'%'` 대신 `'localhost'` 로 잠궈도 됩니다.

### PostgreSQL

```sql
CREATE ROLE myphotos LOGIN PASSWORD '강한_비밀번호';
CREATE DATABASE myphotos OWNER myphotos ENCODING 'UTF8' TEMPLATE template0;
-- (옵션) 별도 schema 격리가 필요하면:
-- \c myphotos
-- CREATE SCHEMA myphotos AUTHORIZATION myphotos;
```

PostgreSQL은 별도 권한 GRANT 없이 OWNER만 지정해도 카탈로그용으로는
충분합니다. 클라이언트(`psql`)에서 한 번 접속 테스트로 권한이 맞는지
먼저 확인하세요.

## 1) 드라이버 설치

### MariaDB / MySQL — `[mariadb]` extra

**Linux / Synology**

```bash
uv pip install --python .venv/bin/python -e ".[mariadb]"
```

**Windows**

```powershell
uv pip install --python .venv\Scripts\python.exe -e ".[mariadb]"
```

순수 Python 드라이버(`PyMySQL`)라 시스템 패키지 빌드 단계 없음.

### PostgreSQL — `[postgres]` extra

**Linux / Synology**

```bash
uv pip install --python .venv/bin/python -e ".[postgres]"
```

**Windows**

```powershell
uv pip install --python .venv\Scripts\python.exe -e ".[postgres]"
```

`psycopg[binary]` (psycopg 3) 를 동봉된 바이너리 wheel 로 설치하므로
`libpq-dev` 같은 시스템 패키지가 필요 없습니다.

## 2) DSN 설정

`config/local.toml`에 추가합니다. 이 파일은 `.gitignore`에 들어있어서
`git pull`로 덮어쓰이지 않습니다.

### MariaDB / MySQL

```toml
[database]
url = "mysql+pymysql://myphotos:강한_비밀번호@DB호스트:3306/myphotos?charset=utf8mb4"
```

### PostgreSQL

```toml
[database]
url = "postgresql+psycopg://myphotos:강한_비밀번호@DB호스트:5432/myphotos"
```

`DB호스트` 자리에는 보통 `localhost`(같은 박스)나 사설망 IP / DNS 이름.
MariaDB의 `charset=utf8mb4`는 이모지/CJK 보존을 위해 꼭 포함하세요.

## 3) 기존 카탈로그 이전 (양방향)

마이그레이션 도구는 **양방향 모두**를 지원합니다 — SQLite → MariaDB,
SQLite → PostgreSQL, MariaDB → SQLite, MariaDB ↔ PostgreSQL, 같은 종류
끼리 등. 앱은 반드시 멈춘 상태에서 실행하세요.

**Linux / Synology**

```bash
sudo systemctl stop myphotos-api myphotos-worker myphotos-ml-worker
```

**Windows**

```powershell
.\scripts\myphotos.ps1 stop
```

### SQLite → MariaDB

**Linux / Synology**

```bash
.venv/bin/python scripts/migrate-db.py \
    sqlite:///data/catalog.db \
    "mysql+pymysql://myphotos:강한_비밀번호@DB호스트:3306/myphotos?charset=utf8mb4" \
    --drop
```

**Windows**

```powershell
.\.venv\Scripts\python.exe scripts\migrate-db.py `
    sqlite:///data/catalog.db `
    "mysql+pymysql://myphotos:강한_비밀번호@DB호스트:3306/myphotos?charset=utf8mb4" `
    --drop
```

### SQLite → PostgreSQL

**Linux / Synology**

```bash
.venv/bin/python scripts/migrate-db.py \
    sqlite:///data/catalog.db \
    "postgresql+psycopg://myphotos:강한_비밀번호@DB호스트:5432/myphotos" \
    --drop
```

**Windows**

```powershell
.\.venv\Scripts\python.exe scripts\migrate-db.py `
    sqlite:///data/catalog.db `
    "postgresql+psycopg://myphotos:강한_비밀번호@DB호스트:5432/myphotos" `
    --drop
```

### 원상복귀 (외부 DB → SQLite)

방향만 바꾸면 됩니다 — src/dst 위치를 swap:

```bash
.venv/bin/python scripts/migrate-db.py \
    "postgresql+psycopg://...@.../myphotos" \
    sqlite:///data/catalog.db \
    --drop
```

> 이전 이름 `scripts/migrate-sqlite-to-mariadb.py` 도 호환을 위해 그대로
> 동작합니다 (내부에서 위 스크립트를 호출).

`--drop`은 대상의 모든 테이블을 비우고 다시 만들므로 첫 이전에만 사용합니다.
스크립트는 끝나는 시점에 source/target 행 수를 비교하여 일치하지 않으면
오류로 종료합니다. 자동 증가 카운터도 dialect별로 알맞게 리셋합니다:

- **MariaDB / MySQL**: `ALTER TABLE ... AUTO_INCREMENT = N`
- **PostgreSQL**: `setval(pg_get_serial_sequence(...), MAX(id), true)`
- **SQLite**: 별도 처리 불필요 (`MAX(rowid)+1` 자동)

마이그레이션 후엔 [2) DSN 설정](#2-dsn-설정)이 그대로 적용된 상태에서
서비스를 다시 시작합니다.

**Linux / Synology**

```bash
sudo systemctl start myphotos-api myphotos-worker myphotos-ml-worker
```

**Windows**

```powershell
.\scripts\myphotos.ps1 start
```

새 설치라면 위 마이그레이션 단계는 생략하고 그냥 `alembic upgrade head`
하면 됩니다 (`database.url`이 설정되어 있으면 자동으로 외부 DB에 스키마
생성됩니다).

### 이전 후 어떤 백엔드를 보고 있는지 확인

```bash
curl -s http://localhost:8888/healthz
```

```json
{
  "db": {
    "backend": "postgresql",
    "dsn": "postgresql+psycopg://myphotos:***@localhost:5432/myphotos"
  }
}
```

`backend` 값이 SQLAlchemy dialect 이름 (`sqlite` / `mysql` /
`postgresql`)으로 정확히 표시되고, DSN에서 비밀번호 자리가 `***`로
마스킹되어 있으면 정상. SQLite로 다시 보이면 `config/local.toml`이 실제
프로젝트 루트에 있고 서비스가 그 인스턴스를 정말 재시작했는지 확인.

## 양쪽이 어떻게 동기화 되나?

**동기화되지 않습니다.** 어느 한 시점엔 한쪽만 "메인"입니다:

- `database.url` 비어있음 → SQLite 가 메인
- `database.url` 설정 → 그 외부 DB 가 메인. SQLite 파일은 옛 스냅샷

이중 쓰기/실시간 복제는 일관성·실패 처리·분산 락이 따라붙어 가정용
NAS에는 과합니다. 대신 백업과 마이그레이션 도구로 같은 효과를 냅니다:

| 시나리오 | 절차 |
| --- | --- |
| **정기 백업** | `scripts/backup-db.sh` (SQLite/MariaDB) 또는 외부 도구. SQLite 모드면 `.db` 스냅샷, MariaDB는 `mysqldump`, PostgreSQL은 `pg_dump` (아래 백업 절 참고). |
| **백엔드 전환** | 위 마이그레이션 스크립트 한 번 + `database.url` 변경 + 재시작. |
| **장애 복구** | 마지막 백업으로 새 인스턴스에 복원, `database.url` 그대로 두고 서비스 시작. |

> 멀티 마스터가 정말 필요해진다면(가족 NAS 규모에서 보통 불필요) MariaDB
> Galera나 PG 논리 복제 등을 구성하고 잡 큐 패턴을 `SELECT ... FOR UPDATE
> SKIP LOCKED` 로 바꿔야 하는데, 그 변경은 의도적으로 미뤄놓은 상태입니다.

## 4) 백업

### SQLite / MariaDB — 내장 스크립트

```bash
# 자동 — local.toml의 URL 따라 알맞은 백업
./scripts/backup-db.sh             # 기본 SQLite
./scripts/backup-db.sh --mariadb   # mysqldump
./scripts/backup-db.sh --both      # 둘 다 (이중 보험)
```

결과는 `data/backups/catalog-YYYYMMDD-HHMMSS.{db,sql.gz}`. 최근 14개씩만
보관합니다. cron / DSM 작업 스케줄러 / Windows 작업 스케줄러로 매일 돌리면 됩니다.

### PostgreSQL — pg_dump 직접

`backup-db.sh` 에 PG 분기가 아직 없으므로 `pg_dump`를 직접 호출하세요:

**Linux / Synology**

```bash
PGPASSWORD='강한_비밀번호' pg_dump \
  -h DB호스트 -p 5432 -U myphotos -d myphotos \
  --format=custom \
  --file=data/backups/catalog-$(date +%Y%m%d-%H%M%S).pgdump
```

**Windows (PowerShell)**

```powershell
$env:PGPASSWORD = '강한_비밀번호'
$ts = Get-Date -Format 'yyyyMMdd-HHmmss'
pg_dump -h DB호스트 -p 5432 -U myphotos -d myphotos `
  --format=custom `
  --file="data\backups\catalog-$ts.pgdump"
$env:PGPASSWORD = $null
```

복원은 `pg_restore`. `.pgdump` 포맷은 압축·병렬 복원·선택적 복원이 가능
합니다.

## 어느 쪽을 골라야 하나

- **SQLite (기본)**: 파일 1개, 별도 서버 불필요, 가족 단위 부하면 충분.
  포팅성 최강 — 디렉토리만 옮기면 됨. **통합 텍스트 검색 (FTS5) 도 정상
  작동.**
- **MariaDB**: 다른 서비스와 같은 DB 서버에 묶고 싶을 때, 정기 백업이
  이미 MariaDB 기준으로 잡혀있을 때. 잘 검증되어 있음.
- **PostgreSQL**: 이미 PG로 통일된 인프라가 있을 때, 또는 PG 전용 기능
  (논리 복제, pgvector 등) 활용을 계획할 때. 위 ‘외부 DB 공통 한계’가
  적용되며 작성자 환경에서는 실측 검증이 부족함.

워커의 잡 큐 패턴(`UPDATE ... WHERE id = (SELECT ... LIMIT 1)`) 은
세 백엔드 모두에서 동작합니다. 코드 분기는 PRAGMA/pool 옵션 + MariaDB
용 `NULLS LAST` 호환 레이어 정도로 좁게 유지되어 있습니다.

---

## English

> [← back to README](../../README.md)

The default is `data/catalog.db` (SQLite, single file) and that's plenty
for almost any household. If you already run a MariaDB (or MySQL) /
PostgreSQL instance on the same NAS and want to fold the photo catalog
into it, swap the backend by setting a DSN.

> **Test status**: SQLite and MariaDB are validated end-to-end (one
> full SQLite → MariaDB migration on a ~70k-photo catalog).
> PostgreSQL ships as a working SQLAlchemy / Alembic target with the
> driver extra wired up and dialect-aware code paths, but the author
> hasn't run a full end-to-end pass on a real PG instance. When
> migrating to PG for the first time, dry-run against a small
> catalog before flipping the main install.

Commands are shown for both:

- **Linux / Synology** (systemd) — `sudo systemctl ...`
- **Windows** (dev PowerShell) — `.\scripts\myphotos.ps1 ...`

## Feature differences on external DB (read first)

Some SQLite-only constructs are used directly by the code. Here's
what changes when you switch to MariaDB or PostgreSQL:

| Feature | Behavior on external DB |
| --- | --- |
| Photo list / filters / sort | **Works**. `NULLS LAST` in `ORDER BY` is handled by a dialect-scoped compiler shim (MariaDB doesn't accept it natively). |
| Year-bucket timeline scrollbar | **Works**. SQLAlchemy `extract("year", ...)` compiles per dialect. |
| Map / clusters / GPS / camera / rating / comment filters | **Works**. Plain SQL. |
| Thumbnails / job queue / workers | **Works**. The claim pattern (`UPDATE ... WHERE id = (SELECT ... LIMIT 1)`) is portable. |
| **Text search bar** | **Returns no results**. FTS5 virtual table + trigram tokenizer is SQLite-only — `fts.is_available()` short-circuits to False on non-SQLite, effectively disabling the search bar. Other filters (date / rating / camera) still work and usually narrow far enough. A LIKE-OR fallback is the documented next step. |

These differences also show up in **Admin → Database** dry-run before
you kick off the actual migration.

> **Problems hit during the first migration (already fixed in code)**
>
> The first SQLite → MariaDB migration surfaced four SQLite vs MariaDB
> differences in sequence — all resolved by alembic 0023–0026 plus
> runtime fixes. New migrations clear them all from the start:
>
> | Error | Cause | Resolution |
> | --- | --- | --- |
> | `1170` (`BLOB/TEXT column used in key spec`) | TEXT column inside a UNIQUE index (`folder_acl.path_prefix`, `photos.rel_path`, `uploads_pending.rel_path`) | 0023, 0024: ALTER to VARCHAR(512) |
> | `1264` (`Out of range value for file_size`) | SQLAlchemy `Integer` → MariaDB `INT(11)` (signed 32-bit, max ~2 GB) overflows on a multi-GB video | 0025: `BigInteger` (BIGINT 64-bit). PG hits the same overflow — fix benefits both |
> | `1062` (`Duplicate entry for rel_path`) | SQLite default comparison is BINARY (case-sensitive); MariaDB default `utf8mb4_unicode_ci` is case-insensitive → `IMG.mov` and `IMG.MOV` collide | 0026: ALTER `rel_path` to `utf8mb4_bin`. PG default is already case-sensitive |
> | `1146` (`Table 'sqlite_master' doesn't exist`) | `fts.is_available()` probed sqlite_master directly | dialect short-circuit — non-SQLite returns False immediately |

## 0) Provision the DB and user

### MariaDB / MySQL

```sql
CREATE DATABASE myphotos
    CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
CREATE USER 'myphotos'@'%' IDENTIFIED BY 'strong_password';
GRANT ALL PRIVILEGES ON myphotos.* TO 'myphotos'@'%';
FLUSH PRIVILEGES;
```

Lock to `'localhost'` instead of `'%'` if the app only connects locally.

### PostgreSQL

```sql
CREATE ROLE myphotos LOGIN PASSWORD 'strong_password';
CREATE DATABASE myphotos OWNER myphotos ENCODING 'UTF8' TEMPLATE template0;
-- (Optional) isolated schema:
-- \c myphotos
-- CREATE SCHEMA myphotos AUTHORIZATION myphotos;
```

PostgreSQL needs no extra GRANT — owning the database is enough for the
catalog. Verify the connection with `psql` first.

## 1) Install the driver

### MariaDB / MySQL — `[mariadb]` extra

**Linux / Synology**

```bash
uv pip install --python .venv/bin/python -e ".[mariadb]"
```

**Windows**

```powershell
uv pip install --python .venv\Scripts\python.exe -e ".[mariadb]"
```

Pure-Python `PyMySQL`, no build step.

### PostgreSQL — `[postgres]` extra

**Linux / Synology**

```bash
uv pip install --python .venv/bin/python -e ".[postgres]"
```

**Windows**

```powershell
uv pip install --python .venv\Scripts\python.exe -e ".[postgres]"
```

`psycopg[binary]` (psycopg 3) ships pre-built binary wheels, so you
don't need `libpq-dev` or similar.

## 2) Configure the DSN

Add to `config/local.toml` (in `.gitignore` so `git pull` won't
overwrite it).

### MariaDB / MySQL

```toml
[database]
url = "mysql+pymysql://myphotos:strong_password@DB_HOST:3306/myphotos?charset=utf8mb4"
```

### PostgreSQL

```toml
[database]
url = "postgresql+psycopg://myphotos:strong_password@DB_HOST:5432/myphotos"
```

`DB_HOST` is usually `localhost` (same box) or a LAN IP / DNS name.
Keep MariaDB's `charset=utf8mb4` for emoji / CJK preservation.

## 3) Migrate the existing catalog (any direction)

The migration tool is **bidirectional** — SQLite ↔ MariaDB ↔
PostgreSQL, in any combination, plus same-dialect copies. Stop the app
first.

**Linux / Synology**

```bash
sudo systemctl stop myphotos-api myphotos-worker myphotos-ml-worker
```

**Windows**

```powershell
.\scripts\myphotos.ps1 stop
```

### SQLite → MariaDB

**Linux / Synology**

```bash
.venv/bin/python scripts/migrate-db.py \
    sqlite:///data/catalog.db \
    "mysql+pymysql://myphotos:strong_password@DB_HOST:3306/myphotos?charset=utf8mb4" \
    --drop
```

**Windows**

```powershell
.\.venv\Scripts\python.exe scripts\migrate-db.py `
    sqlite:///data/catalog.db `
    "mysql+pymysql://myphotos:strong_password@DB_HOST:3306/myphotos?charset=utf8mb4" `
    --drop
```

### SQLite → PostgreSQL

**Linux / Synology**

```bash
.venv/bin/python scripts/migrate-db.py \
    sqlite:///data/catalog.db \
    "postgresql+psycopg://myphotos:strong_password@DB_HOST:5432/myphotos" \
    --drop
```

**Windows**

```powershell
.\.venv\Scripts\python.exe scripts\migrate-db.py `
    sqlite:///data/catalog.db `
    "postgresql+psycopg://myphotos:strong_password@DB_HOST:5432/myphotos" `
    --drop
```

### Rollback (external DB → SQLite)

Same script, swap src/dst:

```bash
.venv/bin/python scripts/migrate-db.py \
    "postgresql+psycopg://...@.../myphotos" \
    sqlite:///data/catalog.db \
    --drop
```

> The legacy name `scripts/migrate-sqlite-to-mariadb.py` still works
> for compatibility (it just calls the script above).

`--drop` truncates and recreates every target table — only use it on
the first migration. The script compares source/target row counts at
the end and exits with an error on any mismatch. Auto-increment
counters are reset per dialect:

- **MariaDB / MySQL**: `ALTER TABLE ... AUTO_INCREMENT = N`
- **PostgreSQL**: `setval(pg_get_serial_sequence(...), MAX(id), true)`
- **SQLite**: implicit (`MAX(rowid) + 1`)

Restart once the migration is done — the DSN from
[step 2](#2-configure-the-dsn) takes effect on boot.

**Linux / Synology**

```bash
sudo systemctl start myphotos-api myphotos-worker myphotos-ml-worker
```

**Windows**

```powershell
.\scripts\myphotos.ps1 start
```

Fresh install path: skip the migration entirely and just
`alembic upgrade head` — with `database.url` set, the schema lands in
the external DB automatically.

### Verify which backend is now live

```bash
curl -s http://localhost:8888/healthz
```

```json
{
  "db": {
    "backend": "postgresql",
    "dsn": "postgresql+psycopg://myphotos:***@localhost:5432/myphotos"
  }
}
```

`backend` reports the SQLAlchemy dialect name (`sqlite` / `mysql` /
`postgresql`); the DSN's password slot is masked as `***`. If you still
see SQLite, double-check that `config/local.toml` sits at the project
root and that the services were actually restarted.

## How are the two backends kept in sync?

**They aren't.** Only one is "primary" at a time:

- `database.url` empty → SQLite is primary.
- `database.url` set → that external DB is primary; the SQLite file is
  an old snapshot.

Live dual-write or streaming replication brings consistency, failure
handling, and distributed locking with it — overkill for a home NAS.
Backups + the migration tool cover the same ground:

| Scenario | Procedure |
| --- | --- |
| **Routine backup** | `scripts/backup-db.sh` (SQLite/MariaDB) or external tooling. SQLite mode → `.db` snapshot; MariaDB → `mysqldump`; PostgreSQL → `pg_dump` (see backup section). |
| **Switch primary backend** | Run the migration script once, edit `database.url`, restart. |
| **Disaster recovery** | Restore the last backup into a fresh instance; keep `database.url`; start the services. |

> If you genuinely need multi-master (unusual at family-NAS scale)
> you'd want MariaDB Galera or PG logical replication, plus a
> `SELECT ... FOR UPDATE SKIP LOCKED` rewrite of the job-queue
> pattern — a deliberately deferred change.

## 4) Backups

### SQLite / MariaDB — built-in script

```bash
# Auto — picks the right backup from local.toml's URL
./scripts/backup-db.sh             # default SQLite
./scripts/backup-db.sh --mariadb   # mysqldump
./scripts/backup-db.sh --both      # both (belt-and-braces)
```

Output goes to `data/backups/catalog-YYYYMMDD-HHMMSS.{db,sql.gz}` with
14-day rotation. Schedule via cron / DSM Task Scheduler / Windows Task
Scheduler.

### PostgreSQL — call pg_dump directly

`backup-db.sh` doesn't have a PG branch yet, so call `pg_dump`
yourself:

**Linux / Synology**

```bash
PGPASSWORD='strong_password' pg_dump \
  -h DB_HOST -p 5432 -U myphotos -d myphotos \
  --format=custom \
  --file=data/backups/catalog-$(date +%Y%m%d-%H%M%S).pgdump
```

**Windows (PowerShell)**

```powershell
$env:PGPASSWORD = 'strong_password'
$ts = Get-Date -Format 'yyyyMMdd-HHmmss'
pg_dump -h DB_HOST -p 5432 -U myphotos -d myphotos `
  --format=custom `
  --file="data\backups\catalog-$ts.pgdump"
$env:PGPASSWORD = $null
```

Restore with `pg_restore`. The `.pgdump` (custom) format supports
compression, parallel restore, and selective restore.

## Which one should I pick?

- **SQLite (default)**: one file, no extra server, plenty for household
  load. Best portability — copy the directory and you're done. **Full-
  text search (FTS5) works as-is.**
- **MariaDB**: when you want the catalog living alongside other
  services in the same DB server, or your existing backups are already
  MariaDB-shaped. Well-exercised.
- **PostgreSQL**: when the rest of your infra is already on PG, or you
  plan to lean on PG-specific features (logical replication, pgvector).
  The shared external-DB caveats above apply, and the author's
  end-to-end testing on PG is light.

The worker's job-queue pattern (`UPDATE ... WHERE id = (SELECT ...
LIMIT 1)`) works on all three backends. Code branching stays narrow:
PRAGMA/pool options plus the MariaDB `NULLS LAST` compatibility shim
in `app/db.py`.

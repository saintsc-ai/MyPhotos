# 외부 DB (MariaDB / MySQL) 사용

> 한국어 / [English](#english)

> [← README로 돌아가기](../../README.md)

기본은 `data/catalog.db` (SQLite, 단일 파일)이며 대부분의 가족 단위
운영에는 충분합니다. 같은 NAS에 이미 돌고 있는 MariaDB / MySQL 인스턴스를
카탈로그용으로 같이 쓰고 싶을 때 DSN을 설정해서 백엔드를 바꿀 수 있습니다.

명령은 두 종류를 함께 보여줍니다:

- **Linux / Synology** (systemd 기반) — `sudo systemctl ...`
- **Windows** (개발용 PowerShell) — `.\scripts\myphotos.ps1 ...`

## 0) DB와 사용자 준비 (MariaDB 측에서)

```sql
CREATE DATABASE myphotos
    CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
CREATE USER 'myphotos'@'%' IDENTIFIED BY '강한_비밀번호';
GRANT ALL PRIVILEGES ON myphotos.* TO 'myphotos'@'%';
FLUSH PRIVILEGES;
```

호스트가 `localhost`로만 접속한다면 `'%'` 대신 `'localhost'`로 잠궈도
됩니다. 클라이언트에서 한 번 접속 테스트해서 권한이 맞는지 먼저 확인하세요.

## 1) MariaDB 드라이버 설치

**Linux / Synology**

```bash
uv pip install --python .venv/bin/python -e ".[mariadb]"
```

**Windows**

```powershell
uv pip install --python .venv\Scripts\python.exe -e ".[mariadb]"
```

순수 Python 드라이버(`PyMySQL`)라 `libmariadb-dev` 같은 시스템 패키지가
필요 없습니다 — Synology DSM과 Windows 모두 빌드 단계 없이 설치됩니다.

## 2) DSN 설정

`config/local.toml`에 추가합니다. 이 파일은 `.gitignore`에 들어있어서
`git pull`로 덮어쓰이지 않습니다.

```toml
[database]
url = "mysql+pymysql://myphotos:강한_비밀번호@DB호스트:3306/myphotos?charset=utf8mb4"
```

`DB호스트` 자리에는 보통 `localhost`(같은 박스)나 사설망 IP / DNS 이름.
`charset=utf8mb4`는 꼭 포함하세요 — 이모지/CJK 보존을 위해 필요합니다.

## 3) 기존 카탈로그 이전 (양방향)

마이그레이션 도구는 **양방향 모두**를 지원합니다 — SQLite → MariaDB,
MariaDB → SQLite, 또는 같은 종류끼리. 앱은 반드시 멈춘 상태에서 실행하세요.

**Linux / Synology**

```bash
sudo systemctl stop myphotos-api myphotos-worker myphotos-ml-worker
```

**Windows**

```powershell
.\scripts\myphotos.ps1 stop
```

### SQLite → MariaDB (가장 흔한 경우)

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

### MariaDB → SQLite (원상복귀)

**Linux / Synology**

```bash
.venv/bin/python scripts/migrate-db.py \
    "mysql+pymysql://myphotos:강한_비밀번호@DB호스트:3306/myphotos?charset=utf8mb4" \
    sqlite:///data/catalog.db \
    --drop
```

**Windows**

```powershell
.\.venv\Scripts\python.exe scripts\migrate-db.py `
    "mysql+pymysql://myphotos:강한_비밀번호@DB호스트:3306/myphotos?charset=utf8mb4" `
    sqlite:///data/catalog.db `
    --drop
```

> 이전 이름 `scripts/migrate-sqlite-to-mariadb.py` 도 호환을 위해 그대로
> 동작합니다 (내부에서 위 스크립트를 호출).

`--drop`은 대상의 모든 테이블을 비우고 다시 만들므로 첫 이전에만 사용합니다.
스크립트는 끝나는 시점에 source/target 행 수를 비교하여 일치하지 않으면
오류로 종료합니다. AUTO_INCREMENT 카운터도 자동으로 끝값+1로 리셋합니다.

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
하면 됩니다 (`database.url`이 설정되어 있으면 자동으로 MariaDB에 스키마
생성됩니다).

### 이전 후 어떤 백엔드를 보고 있는지 확인

```bash
curl -s http://localhost:8888/healthz
```

```json
{
  "db": {
    "backend": "mariadb",
    "dsn": "mysql+pymysql://myphotos:***@localhost:3306/myphotos?charset=utf8mb4"
  }
}
```

`backend`가 `mariadb`로 바뀌어 있고 DSN에서 비밀번호 자리가 `***`로
마스킹되어 있으면 정상. SQLite로 다시 보이면 `config/local.toml`이
실제로 위치한 디렉토리(`config/local.toml`이 프로젝트 루트 기준인지)와
서비스가 그 인스턴스를 정말 재시작했는지 확인하세요.

## 양쪽이 어떻게 동기화 되나?

**동기화되지 않습니다.** 어느 한 시점엔 한쪽만 "메인"입니다:

- `database.url` 비어있음 → SQLite 가 메인, MariaDB 는 (있어도) 무관
- `database.url` 설정 → MariaDB 가 메인, SQLite 파일은 그냥 옛 스냅샷

이중 쓰기/실시간 복제는 일관성·실패 처리·분산 락이 따라붙어 가정용
NAS에는 과합니다. 대신 백업과 마이그레이션 도구로 같은 효과를 냅니다:

| 시나리오 | 절차 |
| --- | --- |
| **정기 백업** | `scripts/backup-db.sh` 를 매일 cron. SQLite 모드면 자동으로 `.db` 스냅샷, MariaDB 모드면 `mysqldump`. |
| **양쪽에 같은 데이터 두기** | `--both`로 백업 1회 → 다른 쪽 DB에 복원 1회. 그 시점부터는 한쪽이 메인이고 다른 쪽은 콜드 스탠바이. |
| **메인 백엔드 전환** | 위 마이그레이션 스크립트 한 번 + `database.url` 변경 + 재시작. |
| **장애 복구** | 마지막 백업으로 새 인스턴스에 복원, `database.url` 그대로 두고 서비스 시작. |

> 멀티 마스터가 정말 필요해진다면(가족 NAS 규모에서 보통 불필요) MariaDB
> Galera 클러스터 등을 구성하고 잡 큐 패턴을 `SELECT ... FOR UPDATE
> SKIP LOCKED` 로 바꿔야 하는데, 그 변경은 의도적으로 미뤄놓은 상태입니다.

## 4) 백업 스크립트

```bash
# 자동 — local.toml의 URL 따라 알맞은 백업
./scripts/backup-db.sh             # 기본 SQLite
./scripts/backup-db.sh --mariadb   # mysqldump
./scripts/backup-db.sh --both      # 둘 다 (이중 보험)
```

결과는 `data/backups/catalog-YYYYMMDD-HHMMSS.{db,sql.gz}`. 최근 14개씩만
보관합니다. cron / DSM 작업 스케줄러 / Windows 작업 스케줄러로 매일 돌리면 됩니다.

## 어느 쪽을 골라야 하나

- **SQLite (기본)**: 파일 1개, 별도 서버 불필요, 가족 단위 부하면 충분.
  포팅성 최강 — 디렉토리만 옮기면 됨.
- **MariaDB**: 다른 서비스와 같은 DB 서버에 묶고 싶을 때, 정기 백업이
  이미 MariaDB 기준으로 잡혀있을 때, 수십만~수백만 장 + 다중 동시 쓰기가
  생길 때. 포팅 시 MariaDB 인스턴스를 같이 챙겨야 함.

워커의 잡 큐 패턴(`UPDATE ... WHERE id = (SELECT ... LIMIT 1)`) 은
양쪽 모두에서 동작하므로 코드 분기는 PRAGMA/pool 옵션 + ORDER BY 의
`NULLS LAST` 호환 레이어 정도뿐입니다.

---

## English

> [← back to README](../../README.md)

The default is `data/catalog.db` (SQLite, single file) and that's plenty
for almost any household. If you already run a MariaDB / MySQL instance
on the same NAS and want to fold the photo catalog into it, swap the
backend by setting a DSN.

Commands are shown for both:

- **Linux / Synology** (systemd) — `sudo systemctl ...`
- **Windows** (dev PowerShell) — `.\scripts\myphotos.ps1 ...`

## 0) Provision the DB and user (on the MariaDB side)

```sql
CREATE DATABASE myphotos
    CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
CREATE USER 'myphotos'@'%' IDENTIFIED BY 'strong_password';
GRANT ALL PRIVILEGES ON myphotos.* TO 'myphotos'@'%';
FLUSH PRIVILEGES;
```

If the app only ever connects from localhost, lock it down to
`'localhost'` instead of `'%'`. Verify the connection with a client
first so permission issues are caught up front.

## 1) Install the MariaDB driver

**Linux / Synology**

```bash
uv pip install --python .venv/bin/python -e ".[mariadb]"
```

**Windows**

```powershell
uv pip install --python .venv\Scripts\python.exe -e ".[mariadb]"
```

Pure-Python driver (`PyMySQL`), so you don't need `libmariadb-dev` or
similar — works straight off on Synology DSM and Windows alike.

## 2) Configure the DSN

Add to `config/local.toml` (already in `.gitignore` so `git pull` won't
overwrite it):

```toml
[database]
url = "mysql+pymysql://myphotos:strong_password@DB_HOST:3306/myphotos?charset=utf8mb4"
```

`DB_HOST` is usually `localhost` (same box) or a private LAN IP / DNS
name. Keep `charset=utf8mb4` — required for emoji and CJK preservation.

## 3) Migrate the existing catalog (both directions)

The migration tool is **bidirectional** — SQLite → MariaDB, MariaDB →
SQLite, or like-to-like. The app must be stopped during the migration.

**Linux / Synology**

```bash
sudo systemctl stop myphotos-api myphotos-worker myphotos-ml-worker
```

**Windows**

```powershell
.\scripts\myphotos.ps1 stop
```

### SQLite → MariaDB (most common path)

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

### MariaDB → SQLite (rollback)

**Linux / Synology**

```bash
.venv/bin/python scripts/migrate-db.py \
    "mysql+pymysql://myphotos:strong_password@DB_HOST:3306/myphotos?charset=utf8mb4" \
    sqlite:///data/catalog.db \
    --drop
```

**Windows**

```powershell
.\.venv\Scripts\python.exe scripts\migrate-db.py `
    "mysql+pymysql://myphotos:strong_password@DB_HOST:3306/myphotos?charset=utf8mb4" `
    sqlite:///data/catalog.db `
    --drop
```

> The legacy name `scripts/migrate-sqlite-to-mariadb.py` still works for
> compatibility (it just calls the script above).

`--drop` truncates and recreates every target table — only use it on
the first migration. The script compares source/target row counts at
the end and exits with an error on any mismatch. AUTO_INCREMENT
counters are reset to (max + 1).

Restart the services once the migration is done — the DSN from
[step 2](#2-configure-the-dsn) takes effect on boot.

**Linux / Synology**

```bash
sudo systemctl start myphotos-api myphotos-worker myphotos-ml-worker
```

**Windows**

```powershell
.\scripts\myphotos.ps1 start
```

For a fresh install you can skip the migration entirely and just run
`alembic upgrade head` — with `database.url` set, the schema lands in
MariaDB automatically.

### Verify which backend is now live

```bash
curl -s http://localhost:8888/healthz
```

```json
{
  "db": {
    "backend": "mariadb",
    "dsn": "mysql+pymysql://myphotos:***@localhost:3306/myphotos?charset=utf8mb4"
  }
}
```

If `backend` reports `mariadb` and the DSN's password slot is masked as
`***`, you're good. If it still shows SQLite, double-check that
`config/local.toml` is where the app expects (project root) and that
the running services were actually restarted.

## How are the two backends kept in sync?

**They aren't.** At any given moment only one is "primary":

- `database.url` empty → SQLite is primary; the MariaDB DB (if any) is
  unrelated.
- `database.url` set → MariaDB is primary; the SQLite file is just an
  old snapshot.

Live dual-write or streaming replication brings consistency, failure
handling, and distributed locking with it — overkill for a home NAS.
Backups + the migration tool cover the same ground:

| Scenario | Procedure |
| --- | --- |
| **Routine backup** | `scripts/backup-db.sh` on a daily cron. SQLite mode → `.db` snapshot; MariaDB mode → `mysqldump`. |
| **Mirror to the other backend** | One `--both` backup → restore once into the other side. From then on one is primary, the other a cold standby. |
| **Switch primary backend** | Run the migration script once, edit `database.url`, restart. |
| **Disaster recovery** | Restore the last backup into a fresh instance; keep `database.url`; start the services. |

> If you genuinely need multi-master (unusual at family-NAS scale)
> you'd want MariaDB Galera and a `SELECT ... FOR UPDATE SKIP LOCKED`
> rewrite of the job-queue pattern — a deliberately deferred change.

## 4) Backup script

```bash
# Auto — picks the right backup from local.toml's URL
./scripts/backup-db.sh             # default SQLite
./scripts/backup-db.sh --mariadb   # mysqldump
./scripts/backup-db.sh --both      # both (belt-and-braces)
```

Output goes to `data/backups/catalog-YYYYMMDD-HHMMSS.{db,sql.gz}` with
14-day rotation. Schedule via cron / DSM Task Scheduler / Windows Task
Scheduler.

## Which one should I pick?

- **SQLite (default)**: one file, no extra server, plenty for household
  load. Best portability — copy the directory and you're done.
- **MariaDB**: when you want the catalog living alongside your other
  services in the same DB server, when you already have MariaDB-style
  backups in place, or when you're heading into hundreds of
  thousands of photos with simultaneous writers. Portability now
  includes moving the MariaDB instance too.

The worker's job-queue pattern (`UPDATE ... WHERE id = (SELECT ...
LIMIT 1)`) works on both backends, so the code branching is mostly
PRAGMA/pool options plus the MariaDB `NULLS LAST` compatibility shim
in `app/db.py`.

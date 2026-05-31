# MyPhotos

![MyPhotos map view](images/map.png)

> 한국어 / [English](#english)

직접 운영하는 사진 카탈로그. 메타데이터 인덱싱과 웹 브라우징을 지원합니다.

- **백엔드**: FastAPI + SQLite (WAL, FTS5, R-Tree)
- **워커 2개**: 인덱싱 워커(스캔/EXIF/썸네일) + ML 워커(객체 검출/CLIP 임베딩/얼굴 검출·클러스터링)
- **저장소**: 기존 사진 폴더는 읽기 전용으로 인덱싱. 썸네일과 DB는 `data/` 아래에 보관
- **자동 분류** (선택): YOLOv8(객체) + CLIP(주제/장면) + YuNet/SFace(얼굴) — 모두 ONNX, CPU 전용
- **대상 호스트**: Synology DSM (DS3622xs+, x86_64), systemd로 실행

## 디렉토리 구조

```text
myphotos/
├── app/                # 애플리케이션 코드
│   ├── api/            # FastAPI 앱 (uvicorn 엔트리)
│   ├── admin/          # 관리용 CRUD (roots, jobs, ml)
│   ├── worker/         # 스캐너 + 인덱싱 잡 러너 (systemd 엔트리)
│   ├── worker_ml/      # ML 잡 러너 — YOLO / CLIP / face (별도 systemd 엔트리)
│   └── web/            # HTMX 템플릿 / 정적 파일
├── config/
│   ├── default.toml    # 기본 설정 (커밋됨)
│   └── local.toml      # 호스트별 오버라이드 (커밋 안 됨)
├── data/               # 런타임 (커밋 안 됨) — DB, 썸네일, 모델, 로그, 휴지통
│   └── models/         # ONNX 모델 (yolo / clip / face) — install-ml-models.sh
├── vendor/             # OS별 바이너리 (exiftool, ffmpeg)
├── alembic/            # DB 마이그레이션
├── scripts/            # 부트스트랩, systemd 설치, ML 모델 다운로드/업로드
└── systemd/            # 유닛 템플릿 (api / worker / ml-worker)
```

## 설치

대상 환경별로 별도 가이드:

| 환경 | 가이드 |
| --- | --- |
| **Synology NAS** (DSM 7.x, systemd) | [docs/install/synology.md](docs/install/synology.md) |
| **Docker** (DSM Container Manager / Linux+Docker / Windows+Docker Desktop) | [docs/install/docker.md](docs/install/docker.md) |
| **일반 Linux** (Debian/Ubuntu/Fedora/Arch + systemd) | [docs/install/linux.md](docs/install/linux.md) |
| **Windows** (개발용) | [docs/install/windows.md](docs/install/windows.md) |

설치가 끝난 뒤의 운영(코드 업데이트 / watcher / 백업 / 트러블슈팅 / 외부 DB / 호스트 이전)은 아래 [설치 후 운영](#설치-후-운영) 섹션에 모아두었습니다 — 어느 환경이든 동일하게 적용됩니다.

## 설치 후 운영

### 코드 업데이트

변경이 없는 단계는 no-op이라 매번 그대로 써도 부작용 없습니다.

```bash
cd ~/myphotos && git pull \
  && uv pip install --python .venv/bin/python -e . \
  && .venv/bin/python -m alembic upgrade head
sudo systemctl restart myphotos-api myphotos-worker myphotos-ml-worker myphotos-watcher
```

활성화하지 않은 유닛이 있으면 그 토큰은 빼세요 — 존재하지 않는 유닛
재시작 시 에러. (예: ML 워처/watcher 안 켰으면 `myphotos-api myphotos-worker`만)

#### 단계별로 (각 단계가 언제 필요한지)

| 단계 | 명령 | 필요한 때 |
| --- | --- | --- |
| 1. 코드 받기 | `git pull` | 항상 |
| 2. 의존성 동기화 | `uv pip install --python .venv/bin/python -e .` | `pyproject.toml` 변경 시 (새 라이브러리/버전 핀 등) |
| 3. DB 마이그레이션 | `.venv/bin/python -m alembic upgrade head` | `alembic/versions/` 에 새 파일 추가 시 |
| 4. 서비스 재시작 | `sudo systemctl restart myphotos-api myphotos-worker myphotos-ml-worker` | 코드/설정/스키마 어떤 것이든 바뀌었으면 |

확인 — 어떤 단계가 진짜 필요했는지는 `git diff --stat HEAD@{1}` 으로 한 번에 보입니다.

#### 동작 검증

```bash
sudo systemctl status myphotos-api myphotos-worker myphotos-ml-worker
```

```bash
curl -s http://localhost:8888/healthz | python3 -m json.tool
```

```bash
sudo journalctl -u myphotos-api -n 20 --no-pager
```

`/healthz` 응답의 `version` 이 새 값으로 바뀌고, status가 셋 다
`active (running)` 이면 성공.

#### 브라우저 캐시

프론트(`index.html`, `admin.html`) 변경된 commit이 섞여있는데도 UI가
그대로면 브라우저 캐시 때문입니다 — 강제 새로고침 (`Ctrl+Shift+R`,
모바일은 주소창 당겨서 새로고침).

#### 외부 바이너리 업데이트 (드물게)

`exiftool`/`ffmpeg` 새 버전을 받으려면:

```bash
./scripts/install-vendor-linux-x64.sh
sudo systemctl restart myphotos-worker
```

ML 모델은 한 번 받으면 거의 갱신 안 되지만 새 모델 commit이 있으면:

```bash
./scripts/install-ml-models.sh
sudo systemctl restart myphotos-ml-worker
```

#### 롤백

뭐가 잘못된 것 같으면 이전 commit으로 되돌리기.

먼저 직전 commit 해시 확인:

```bash
git log --oneline -10
```

원하는 해시로 리셋하고 의존성/스키마 정리 (스키마 downgrade는 정말
스키마도 되돌릴 때만):

```bash
git reset --hard <hash>
uv pip install --python .venv/bin/python -e .
.venv/bin/python -m alembic downgrade -1
sudo systemctl restart myphotos-api myphotos-worker myphotos-ml-worker
```

⚠️ `alembic downgrade` 는 데이터 손실 가능성이 있는 마이그레이션이면
실패할 수 있습니다. 그땐 백업(`scripts/backup-db.sh` 로 미리 떠둔
파일)을 복원하는 게 안전합니다.

#### 정기 백업 (cron / DSM 작업 스케줄러)

DSM **제어판 → 작업 스케줄러 → 사용자 정의 스크립트** 에 매일:
```bash
/var/services/homes/<user>/myphotos/scripts/backup-db.sh
```
`data/backups/` 에 최근 14개 자동 보관됩니다.

### 사진 폴더에서 직접 파일을 옮기거나 지우면 어떻게 되나

워커는 정해진 주기(기본 매일)와 관리 → 사진 폴더의 **스캔** 버튼으로
풀스캔을 돌립니다. 풀스캔이 같은 root를 처음부터 끝까지 훑으면서:

| 변경 종류 | 처리 |
| --- | --- |
| **새 파일 추가** | 행 신규 추가 + 인덱싱 잡 (해시/EXIF/썸네일) |
| **내용 변경** (size·mtime 바뀜) | `content_signature` 불일치 감지 → EXIF/썸네일 재처리 |
| **파일 삭제** | 같은 경로가 walk 결과에 없음 → `status='missing'` 으로 자동 마킹. 갤러리/지도/검색/중복에서 즉시 사라짐. DB 행(평점·코멘트·태그·공유링크 등)은 보존 |
| **파일 이름 변경** | 옛 경로는 missing, 새 경로는 신규 추가. 같은 sha256이면 라이트박스의 ⚏ 중복 칩에서 두 행이 같은 파일임이 보임 |
| **폴더 이름 변경 / 이동** | 같은 패턴 — 옛 위치 전부 missing, 새 위치 전부 신규 |
| **권한 갑자기 막힘** | scandir 실패 로그 기록, 행은 그대로 (false missing 방지). 다음 정상 스캔에서 일관성 회복 |
| **사라졌던 파일 다시 나타남** | 같은 경로에 동일 파일 발견 시 자동 복구 (`missing` → `active`) |

이 reconciliation은 **풀스캔(`limit` 없이) 에서만** 동작합니다. 200장
샘플 스캔은 자기가 보지 못한 파일이 지워졌다고 판단하면 위험하니까요.

**실시간 감지(watchdog) — 선택적 활성화**

기본은 daily 풀스캔 + 수동 트리거. 변경을 즉시 반영하고 싶으면 별도
워처 서비스를 켤 수 있습니다. inotify로 root를 구독하고, 변경 이벤트가
30초 동안(설정 가능) 잠잠해지면 그 root에 `discover_root` 잡을 자동
enqueue합니다.

켜는 법:

```bash
# 1. config/local.toml 에 추가
[watcher]
enabled = true
# debounce_seconds = 30          # 기본값
# reconcile_roots_seconds = 60   # 기본값
```

```bash
# 2. systemd 유닛 설치 (install-systemd.sh가 *.service.in 다 잡음)
./scripts/install-systemd.sh
sudo systemctl enable myphotos-watcher
```

```bash
sudo systemctl start  myphotos-watcher
```

```bash
sudo systemctl status myphotos-watcher
```

```bash
sudo journalctl -u myphotos-watcher -f
```

inotify watch 한도 (10만+ 폴더면 필요):

```bash
echo "fs.inotify.max_user_watches=524288" | sudo tee -a /etc/sysctl.conf
```

```bash
echo "fs.inotify.max_user_instances=512"  | sudo tee -a /etc/sysctl.conf
```

```bash
sudo sysctl -p
```

```bash
# 확인
find /volume1/photo -type d | wc -l                  # 등록할 폴더 수
cat /proc/sys/fs/inotify/max_user_watches            # 한도
```

> ⚠️ **한계** — inotify는 호스트 OS 파일시스템 변경만 감지합니다.
> 외부에서 SMB로 접속해 변경하는 것은 DSM의 samba 데몬이 쓰는
> 것이므로 보통 잡힙니다. 외부 NAS의 NFS 마운트, S3FS 같은 가상
> 파일시스템은 못 잡습니다 — 그쪽은 daily 풀스캔이 백업입니다.

#### 동작 상태 확인 (watcher 진단)

**1. systemd 단의 살아있음** — `Active: active (running)` 이어야 함:

```bash
sudo systemctl status myphotos-watcher
```

**2. 부팅 로그** — 구독한 root 수 / 도구 감지 / catch-up. 정상이면
`"watcher observer started"`, `"watcher: subscribed root id=1 (/volume1/photo)"`,
`"watcher: catch-up touched 1 root(s)"`가 떠야 함:

```bash
sudo journalctl -u myphotos-watcher -n 50 --no-pager
```

**3. 실시간 로그** — 파일 추가/변경 시 이벤트 흐름 보기. 사진 폴더에
파일 한 개 던지고 ~30초 후 `"watcher: enqueued discover_root for root id=N"`
떠야 정상:

```bash
sudo journalctl -u myphotos-watcher -f
```

**4. API에서 한 줄** — 별도 SSH 없이 확인 가능. `watcher` 블록의
`alive_at`(최근 heartbeat 시각), `age_seconds`(몇 초 전), `stale`(true면
15초 이상 무응답), `watched_root_ids`, `pending_roots` 확인:

```bash
curl -s http://localhost:8888/healthz | python3 -m json.tool
```

자주 막히는 케이스:

| 증상 | 원인 / 해결 |
| --- | --- |
| `watcher disabled in config (watcher.enabled=false)` 후 종료 | `config/local.toml`에 `[watcher] enabled = true` 추가 후 재시작 |
| `Active: active (running)` 인데 `/healthz` `stale: true` | 프로세스는 살았지만 dispatcher가 멈춤 — `journalctl -u myphotos-watcher --since "10 min ago"` 로 traceback 확인 |
| `schedule failed ... No space left on device` | `fs.inotify.max_user_watches` 한도 초과. 위 sysctl 명령으로 늘리기 |
| `watched_root_ids: []` | DB에 enabled root 없음. 관리 → 사진 폴더에서 enable, 또는 root 추가 |
| 이벤트 발생해도 `enqueued discover_root` 안 뜸 | (1) ignore 패턴에 걸림 (.tmp, @eaDir 등), (2) 30초 debounce 대기 중, (3) 기존 discover_root 잡 inflight 중 |

### 포트 변경

`config/local.toml`에:
```toml
[server]
port = 9000
```

그 후 API 재시작:

```bash
sudo systemctl restart myphotos-api
```

`myphotos-api.service`의 ExecStart에 포트가 박혀 있다면
`./scripts/install-systemd.sh` 재실행.

### 로그 보기
```bash
sudo journalctl -u myphotos-api    -n 60 --no-pager
```

```bash
sudo journalctl -u myphotos-worker -f
```

### 문제 해결

| 증상 | 확인 / 해결 |
| --- | --- |
| 사진 폴더 root가 **`접근 불가`** | Synology Photos가 만든 폴더는 보통 `d---------+` (ACL 전용)이라 systemd가 실행하는 `$USER` 계정으론 못 읽음. `ls -la /volume1/photo`로 확인하고 `sudo chmod 755 /volume1/photo` (또는 9단계의 `synoacltool` ACL 추가). |
| 회전·삭제 시 **`Permission denied`** / **`Error creating file: ..._exiftool_tmp`** | 디렉토리 쓰기 권한 부족. exiftool은 같은 폴더에 임시 파일을 만들고, 삭제는 폴더에서 파일 entry를 지워야 함. 9단계 표의 두 번째 줄(트리 전체 `chmod -R u+rwX,g+rX,o+rX`) 적용. `ls -ld /volume1/photo/2024년사진/`로 디렉토리에 `w`가 있는지 확인. |
| 삭제한 사진이 **새로고침하면 다시 나타남** | 휴지통 이동이 실패했는데도 (권한 부족 등) UI에서 사라졌다가, 다음 스캐너 패스가 원래 폴더의 파일을 발견하고 `status='active'`로 부활시킴. v0.x부터는 실패 사유를 alert로 surface하고 DB 상태도 그대로 유지함 (위 권한 문제 해결 필요). |
| 잡 큐에 잡이 계속 쌓이고 줄지 않음 | 워커가 죽었거나 이전 잘못된 잡들이 큐를 막고 있을 수 있음. `sudo systemctl status myphotos-worker`로 워커 살아있는지 확인 → 죽었으면 `sudo journalctl -u myphotos-worker -n 60`. 큐 비우려면 관리 → 색인 → 잡 큐 → "대기·실패 잡 비우기" 또는 CLI `curl -X POST http://localhost:8888/api/admin/jobs/purge -H "Content-Type: application/json" -d '{"include_running":true}'`. |
| 타임라인이 비거나 500 오류 | `alembic current`가 `(head)`인지 확인. 아니면 `alembic upgrade head` 후 재시작 |
| 색인이 너무 느림 | 관리 → 설정 → 워커 → `concurrency` 조정. HDD면 3~4가 더 빠를 수 있음 |
| 워커 좀비 (status에 두 개 떠 있음) | `ps -ef \| grep app.worker`로 확인 후 systemd 외부 프로세스 `kill` |
| ML 워커가 active되자마자 죽음 | `journalctl -u myphotos-ml-worker -n 30`에 `model missing` 있으면 `./scripts/install-ml-models.sh` 미실행. 받은 후 재시작 |
| ML 분류 잡 다수가 failed | 모델 출력 형식이 코드 기대와 다른 변종일 수 있음. 위 로그의 traceback과 함께 이슈 등록 |
| admin 비밀번호 잊음 | `.venv/bin/python -c "from app.auth import hash_password; print(hash_password('새비번'))"` → 출력 해시를 sqlite3로 `UPDATE users SET password_hash='<해시>' WHERE username='admin';` |

## 외부 DB (MariaDB) 사용 (선택)

기본은 `data/catalog.db` (SQLite, 단일 파일)이며 대부분의 가족 운영
환경에선 충분합니다. 기존 NAS의 MariaDB를 카탈로그로 같이 쓰고 싶다면
DSN을 설정해서 백엔드를 바꿀 수 있습니다.

### 0) DB와 사용자 준비 (MariaDB 측에서)

```sql
CREATE DATABASE myphotos
    CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
CREATE USER 'myphotos'@'%' IDENTIFIED BY '강한_비밀번호';
GRANT ALL PRIVILEGES ON myphotos.* TO 'myphotos'@'%';
FLUSH PRIVILEGES;
```

### 1) MariaDB 드라이버 설치

```bash
uv pip install --python .venv/bin/python -e ".[mariadb]"
```

순수 Python 드라이버(`PyMySQL`)라 `libmariadb-dev` 같은 시스템 패키지가
필요 없습니다.

### 2) DSN 설정

`config/local.toml`에 추가:

```toml
[database]
url = "mysql+pymysql://myphotos:강한_비밀번호@DB호스트:3306/myphotos?charset=utf8mb4"
```

### 3) 기존 카탈로그 이전 (양방향)

마이그레이션 도구는 **양방향 모두**를 지원합니다 — SQLite → MariaDB,
MariaDB → SQLite, 또는 같은 종류끼리. 앱은 반드시 멈춘 상태에서 실행하세요.

```bash
sudo systemctl stop myphotos-api myphotos-worker myphotos-ml-worker
```

**SQLite → MariaDB (가장 흔한 경우)**
```bash
.venv/bin/python scripts/migrate-db.py \
    sqlite:///data/catalog.db \
    "mysql+pymysql://myphotos:강한_비밀번호@DB호스트:3306/myphotos?charset=utf8mb4" \
    --drop
```

**MariaDB → SQLite (원상복귀)**
```bash
.venv/bin/python scripts/migrate-db.py \
    "mysql+pymysql://myphotos:강한_비밀번호@DB호스트:3306/myphotos?charset=utf8mb4" \
    sqlite:///data/catalog.db \
    --drop
```

> 이전 이름 `scripts/migrate-sqlite-to-mariadb.py` 도 호환을 위해 그대로
> 동작합니다 (내부에서 위 스크립트를 호출).

`--drop`은 대상의 모든 테이블을 비우고 다시 만들므로 첫 이전에만 사용합니다.
스크립트는 끝나는 시점에 source/target 행 수를 비교하여 일치하지 않으면
오류로 종료합니다. AUTO_INCREMENT 카운터도 자동으로 끝값+1로 리셋합니다.

마이그레이션 후엔 `config/local.toml` 의 `database.url` 을 새 백엔드에
맞춰 수정한 뒤 서비스를 다시 시작합니다.

```bash
sudo systemctl start myphotos-api myphotos-worker myphotos-ml-worker
```

새 설치라면 위 마이그레이션 단계는 생략하고 그냥 `alembic upgrade head`
하면 됩니다 (`database.url`이 설정되어 있으면 자동으로 MariaDB에 스키마
생성됩니다).

### 양쪽이 어떻게 동기화 되나?

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

### 4) 백업 스크립트

```bash
# 자동 — local.toml의 URL 따라 알맞은 백업
./scripts/backup-db.sh             # 기본 SQLite
./scripts/backup-db.sh --mariadb   # mysqldump
./scripts/backup-db.sh --both      # 둘 다 (이중 보험)
```

결과는 `data/backups/catalog-YYYYMMDD-HHMMSS.{db,sql.gz}`. 최근 14개씩만
보관합니다. cron / DSM 작업 스케줄러로 매일 돌리면 됩니다.

### 어느 쪽을 골라야 하나

- **SQLite (기본)**: 파일 1개, 별도 서버 불필요, 가족 단위 부하면 충분.
  포팅성 최강 — 디렉토리만 옮기면 됨.
- **MariaDB**: 다른 서비스와 같은 DB 서버에 묶고 싶을 때, 정기 백업이
  이미 MariaDB 기준으로 잡혀있을 때, 수십만~수백만 장 + 다중 동시 쓰기가
  생길 때. 포팅 시 MariaDB 인스턴스를 같이 챙겨야 함.

워커의 잡 큐 패턴(`UPDATE ... WHERE id = (SELECT ... LIMIT 1)`) 은
양쪽 모두에서 동작하므로 코드 분기는 PRAGMA/pool 옵션 정도뿐입니다.
## 다른 호스트로 이전 (재인덱싱 없이)

다른 NAS로 이전해도 **재인덱싱 없이** 그대로 사용 가능합니다. 썸네일은
SHA-256으로 주소되고, `photos.rel_path`는 root 기준 상대 경로(POSIX/NFC)로
저장되어 있어 호스트별로 바뀌는 건 `roots.abs_path` 하나뿐입니다.

### 1) 원본 호스트 — 정합성 있는 스냅샷

```bash
sudo systemctl stop myphotos-api myphotos-worker
```

```bash
sqlite3 ~/myphotos/data/catalog.db ".backup ~/myphotos/data/catalog.db.snapshot"
```

> WAL 모드라 서비스 정지 없이 그대로 `data/`를 복사하면
> `catalog.db-wal`이 어중간한 상태일 수 있습니다. 위처럼 정지 → backup
> 한 번 → 전송이 안전합니다.

### 2) 새 호스트로 전송

`data/` 통째로 + `config/local.toml` 두 가지만 옮기면 됩니다.

```bash
# 환경에 맞게 두 변수 채우기
NEW_HOST="newnas.local"          # 새 NAS 주소 (또는 IP)
NEW_USER="$USER"                 # 새 NAS 쪽 사용자명 (보통 같은 ID)

# data/ 전체 (catalog.db, thumbs/, session.secret, trash/, logs/)
rsync -aP ~/myphotos/data/ \
  "$NEW_USER@$NEW_HOST:~/myphotos/data/"

# 호스트별 설정 (secret_key 포함 — 같은 키를 가져가면 기존 세션도 유지)
rsync -aP ~/myphotos/config/local.toml \
  "$NEW_USER@$NEW_HOST:~/myphotos/config/local.toml"
```

### 3) 새 호스트 — 셋업

```bash
# 코드는 새로 clone (vendor/와 .venv는 OS별이므로 재생성)
git clone git@github.com:saintsc-ai/MyPhotos.git ~/myphotos

# data/ 와 config/local.toml은 위 2)에서 이미 자리잡고 있음
cd ~/myphotos
./scripts/bootstrap.sh                       # Python venv
./scripts/install-vendor-linux-x64.sh        # exiftool / ffmpeg (OS별 바이너리)
./scripts/install-systemd.sh
sudo systemctl enable myphotos-api myphotos-worker
```

```bash
sudo systemctl start  myphotos-api myphotos-worker
```

### 4) 사진 폴더 경로 갱신

원본 NAS에서 `/volume1/photo`였던 root가 새 호스트에서는
`/mnt/data/photos`처럼 바뀌었을 수 있습니다. 관리 페이지에서 수정:

1. 브라우저로 `http://새-호스트:8888/admin.html` 접속
2. **사진 폴더** 탭 → 해당 루트 행의 **`경로`** 버튼 클릭
3. 새 절대 경로 입력 → 저장

루트의 **라벨은 그대로 유지**되고, `photos.rel_path`(상대 경로)도 그대로이므로
이 한 가지만 바꾸면 모든 사진이 다시 연결됩니다.

또는 curl로:

```bash
curl -b cookies -X PATCH http://newnas:8888/api/admin/roots/1 \
  -H "Content-Type: application/json" \
  -d '{"abs_path":"/mnt/data/photos"}'
```

### 5) 검증

관리 → **색인** 탭에서 EXIF/썸네일 진행률이 이전 NAS의 값과 동일한지 확인.
만약 일부가 `missing`으로 바뀌었다면 그건 root 안 내부 폴더 구조가
달라진 사진들 — 디스커버리를 한 번 돌리면(`시험` 버튼) `missing` 또는
`active`로 재정리됩니다.

### 옮기지 않는 것

| 항목 | 이유 |
| --- | --- |
| `vendor/<os-arch>/` | exiftool/ffmpeg는 OS별 바이너리. 새 호스트에서 `install-vendor-*.sh`로 재설치 |
| `.venv/` | Python venv도 호스트별. `bootstrap.sh`가 새로 만듦 |
| `*.db-wal`, `*.db-shm` | WAL 부속 파일은 backup 명령 이후 자동 흡수됨 |

### 옮기지 않으면 일어나는 일

| 빠뜨림 | 결과 |
| --- | --- |
| `data/catalog.db` | 전부 재색인 (몇 시간) |
| `data/thumbs/` | DB는 살아있지만 모든 썸네일 재생성 |
| `data/session.secret` | 새 키 자동 생성 → 모든 사용자 재로그인 |
| `config/local.toml` | 기본값으로 동작 (secret_key는 자동 생성). 별도 튜닝은 다시 설정 |

DB는 단일 SQLite 파일이며, 외부 서비스는 필요 없습니다.

---

## English

Self-hosted photo catalog with metadata indexing and web browsing.

- **Backend**: FastAPI + SQLite (WAL, FTS5, R-Tree)
- **Two workers**: indexing (scanning / EXIF / thumbnails) and ML (object detection / CLIP embeddings / face detection + clustering), each as its own systemd unit
- **Storage**: indexes existing folders read-only; thumbnails and DB live inside `data/`
- **Auto-classification** (optional): YOLOv8 (objects) + CLIP (topics/scenes) + YuNet/SFace (faces) — all ONNX, CPU only
- **Target host**: Synology DSM (DS3622xs+, x86_64) via systemd

## Layout

```text
myphotos/
├── app/                # application code
│   ├── api/            # FastAPI app (uvicorn entry)
│   ├── admin/          # admin CRUD (roots, jobs, ml)
│   ├── worker/         # scanner + indexing job runner (systemd entry)
│   ├── worker_ml/      # ML job runner — YOLO / CLIP / face (separate systemd entry)
│   └── web/            # HTMX templates / static
├── config/
│   ├── default.toml    # built-in defaults (tracked)
│   └── local.toml      # per-host overrides (NOT tracked)
├── data/               # runtime (NOT tracked) — DB, thumbs, models, logs, trash
│   └── models/         # ONNX weights (yolo / clip / face) — install-ml-models.sh
├── vendor/             # OS-specific binaries (exiftool, ffmpeg)
├── alembic/            # DB migrations
├── scripts/            # bootstrap, systemd install, ML model download/upload
└── systemd/            # unit templates (api / worker / ml-worker)
```

## Install

Pick the guide that matches your environment:

| Environment | Guide |
| --- | --- |
| **Synology NAS** (DSM 7.x, systemd) | [docs/install/synology.md](docs/install/synology.md) |
| **Docker** (DSM Container Manager / Linux+Docker / Windows+Docker Desktop) | [docs/install/docker.md](docs/install/docker.md) |
| **Generic Linux** (Debian/Ubuntu/Fedora/Arch + systemd) | [docs/install/linux.md](docs/install/linux.md) |
| **Windows** (dev) | [docs/install/windows.md](docs/install/windows.md) |

Post-install ops (code update / watcher / backup / troubleshooting / external DB / host porting) are kept in the [Post-install](#post-install) section below — they apply equally to every environment.

## Post-install

### Updating the code

Pre-restart steps (no-op when nothing changed):

```bash
cd ~/myphotos && git pull && uv pip install --python .venv/bin/python -e . && .venv/bin/python -m alembic upgrade head
```

Then restart the services:

```bash
sudo systemctl restart myphotos-api myphotos-worker
```

### Troubleshooting

| Symptom | Check / fix |
| --- | --- |
| Root row shows **`접근 불가`** (no access) | Synology Photos folders are usually `d---------+` (ACL-only) and unreadable by the systemd `$USER`. `ls -la /volume1/photo` to confirm, then `sudo chmod 777 /volume1/photo` (or the `synoacltool` ACL entry from step 9). |
| Queue keeps growing, jobs aren't progressing | Worker may be dead, or stale jobs from a bad earlier run are blocking. Check `sudo systemctl status myphotos-worker`; if it's running, purge the queue via Admin → 색인 → 잡 큐 → "대기·실패 잡 비우기", or `curl -X POST http://localhost:8888/api/admin/jobs/purge -H "Content-Type: application/json" -d '{"include_running":true}'`. |
| Empty timeline or 500 errors | `alembic current` should end in `(head)`; if not, `alembic upgrade head` and restart |
| Slow indexing | 관리 → 설정 → worker → `concurrency`. HDD storage often goes faster at 3–4 than 6+ |
| Two worker processes (status shows it) | `ps -ef \| grep app.worker`; `kill` any not under systemd |
| Forgot admin password | `.venv/bin/python -c "from app.auth import hash_password; print(hash_password('new_pw'))"`, then `sqlite3 data/catalog.db "UPDATE users SET password_hash='<hash>' WHERE username='admin';"` |

## Porting to a new host (without re-indexing)

Moving the catalog to a different NAS keeps every photo's index intact —
**no re-indexing required**. Thumbnails are addressed by SHA-256 and
`photos.rel_path` is stored as a POSIX/NFC path relative to the root,
so the only host-specific value is `roots.abs_path`.

### 1) Source host — consistent snapshot

```bash
sudo systemctl stop myphotos-api myphotos-worker
```

```bash
sqlite3 ~/myphotos/data/catalog.db ".backup ~/myphotos/data/catalog.db.snapshot"
```

> WAL mode means a hot-copy of `data/` may include a half-written
> `catalog.db-wal`. Stopping the services first (or using `.backup`)
> avoids that.

### 2) Transfer to the new host

Two trees to copy: the whole `data/` directory and `config/local.toml`.

```bash
# Fill in these two for your environment
NEW_HOST="newnas.local"          # new NAS address (or IP)
NEW_USER="$USER"                 # account on the new NAS (often same)

# Runtime state — DB, thumbnails, session secret, trash, logs
rsync -aP ~/myphotos/data/ \
  "$NEW_USER@$NEW_HOST:~/myphotos/data/"

# Host config — same secret_key keeps existing sessions valid
rsync -aP ~/myphotos/config/local.toml \
  "$NEW_USER@$NEW_HOST:~/myphotos/config/local.toml"
```

### 3) New host — set up

```bash
# Fresh checkout (vendor/ and .venv are OS-specific, regenerated below)
git clone git@github.com:saintsc-ai/MyPhotos.git ~/myphotos

# data/ and config/local.toml are already in place from step 2.
cd ~/myphotos
./scripts/bootstrap.sh                       # Python venv
./scripts/install-vendor-linux-x64.sh        # exiftool / ffmpeg
./scripts/install-systemd.sh
sudo systemctl enable myphotos-api myphotos-worker
```

```bash
sudo systemctl start  myphotos-api myphotos-worker
```

### 4) Point the root at the new path

The old `/volume1/photo` likely lives at a different mount point on the
new host (e.g. `/mnt/data/photos`). Update it via the admin UI:

1. Open `http://new-host:8888/admin.html`
2. **사진 폴더 (Photo folders)** tab → click **`경로`** on the root row
3. Enter the new absolute path → save

The label stays the same and every photo's `rel_path` (relative path)
is unchanged, so this single edit reconnects everything.

Or via curl:

```bash
curl -b cookies -X PATCH http://newnas:8888/api/admin/roots/1 \
  -H "Content-Type: application/json" \
  -d '{"abs_path":"/mnt/data/photos"}'
```

### 5) Verify

Admin → **색인 (Indexing)** tab — the EXIF and thumbnail progress
counters should match the source host. If a subset has flipped to
`missing`, those are photos whose path within the root changed; a
discover run (sample-scan button on the root) will reconcile them
to `active` or `missing` again.

### Things NOT to copy

| Item | Why |
| --- | --- |
| `vendor/<os-arch>/` | exiftool/ffmpeg are OS-specific. Re-install via `install-vendor-*.sh` |
| `.venv/` | Python venv is host-specific. `bootstrap.sh` rebuilds it |
| `*.db-wal`, `*.db-shm` | WAL side files are absorbed by `.backup` |

### What happens if you skip a piece

| Missed | Consequence |
| --- | --- |
| `data/catalog.db` | Full re-index (several hours) |
| `data/thumbs/` | DB intact, every thumbnail regenerates |
| `data/session.secret` | New key auto-generated → every user must log in again |
| `config/local.toml` | Defaults take over (secret auto-generated); custom tuning lost |

The DB is a single SQLite file. No external services required.

# MyPhotos

> 한국어 / [English](#english)

직접 운영하는 사진 카탈로그. 메타데이터 인덱싱과 웹 브라우징을 지원합니다.

- **백엔드**: FastAPI + SQLite (WAL, FTS5, R-Tree)
- **워커**: 스캔, EXIF 추출, 썸네일 생성을 담당하는 별도 프로세스
- **저장소**: 기존 사진 폴더는 읽기 전용으로 인덱싱. 썸네일과 DB는 `data/` 아래에 보관
- **대상 호스트**: Synology DSM (DS3622xs+, x86_64), systemd로 실행

## 디렉토리 구조

```text
myphotos/
├── app/                # 애플리케이션 코드
│   ├── api/            # FastAPI 앱 (uvicorn 엔트리)
│   ├── admin/          # 관리용 CRUD (roots, jobs)
│   ├── worker/         # 스캐너 + 잡 러너 (systemd 엔트리)
│   └── web/            # HTMX 템플릿 / 정적 파일
├── config/
│   ├── default.toml    # 기본 설정 (커밋됨)
│   └── local.toml      # 호스트별 오버라이드 (커밋 안 됨)
├── data/               # 런타임 (커밋 안 됨) — DB, 썸네일, 로그, 휴지통
├── vendor/             # OS별 바이너리 (exiftool, ffmpeg)
├── alembic/            # DB 마이그레이션
├── scripts/            # 부트스트랩, systemd 설치
└── systemd/            # 유닛 템플릿
```

## 설치 (Synology NAS — 단계별)

> 아래 명령들은 모두 `~`(현재 사용자의 홈)와 `$USER`(현재 사용자명)를
> 사용하므로, 어떤 DSM 계정으로 로그인했든 그대로 복사·실행하면 됩니다.
> DSM의 사용자 홈은 보통 `/var/services/homes/$USER`인데 셸의 `~`가
> 이를 자동으로 가리킵니다.
>
> 설치 폴더 이름(여기서는 `myphotos`)도 원하는 이름으로 바꾸셔도 됩니다 —
> 이하 명령에서 `~/myphotos` 부분만 그에 맞춰 바꾸세요.

### 사전 준비

| 항목 | 비고 |
| --- | --- |
| DSM 사용자 계정 | 어떤 ID든 OK. `sudo` 권한 필요 (systemd 유닛 설치 시) |
| SSH 접근 | DSM 제어판 → 터미널 및 SNMP → SSH 활성 |
| 인터넷 | uv / 의존성 / vendor 바이너리 다운로드용 |
| 사진 root 폴더 | 예: `/volume1/photo`. 사용자에게 읽기 권한 |
| 8888 포트 | 다른 서비스가 안 쓰면 그대로. 점유 시 [설정](#설치-후-운영) 참고 |

### 0) uv 설치 (1회만)

[uv](https://docs.astral.sh/uv/)는 Python 버전 + venv를 한 번에 관리하는 도구입니다.

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
source ~/.bashrc          # PATH 즉시 반영
uv python install 3.11.9  # 사용자 영역에 Python 3.11.9 설치
```

검증:
```bash
uv --version              # → uv 0.x.y
```

### 1) 코드 받기

```bash
cd ~
git clone https://github.com/saintsc-ai/MyPhotos.git myphotos
cd ~/myphotos
```

> 폴더 이름을 다르게 쓰고 싶다면 (예: `photo-server`) clone 끝의 인자를
> 바꾸세요: `git clone <URL> photo-server`. 이후 `~/photo-server`로 cd.

### 2) Python venv + 라이브러리 설치

```bash
./scripts/bootstrap.sh
```

스크립트가 자동으로:
- `.venv/`에 Python 3.11 가상환경 생성 (uv가 있으면 사용, 없으면 시스템 python)
- `pyproject.toml` 의존성 설치 (`fastapi`, `sqlalchemy`, `bcrypt`, `pillow` 등)

검증:
```bash
.venv/bin/python --version    # → Python 3.11.x
```

### 3) exiftool / ffmpeg 설치 (RAW / HEIC / 동영상 썸네일용)

```bash
./scripts/install-vendor-linux-x64.sh
```

`vendor/linux-x64/`에 두 바이너리가 들어갑니다. 시스템 PATH에 이미 있으면
이 단계는 건너뛰어도 되지만, 호스트 이전 시 같이 옮길 수 있어 편합니다.

검증:
```bash
./vendor/linux-x64/exiftool -ver
./vendor/linux-x64/ffmpeg -version | head -1
```

### 4) (선택) HEIC 직접 열기 활성화

iPhone HEIC를 Pillow로 직접 열어 더 빠르게 처리하고 싶을 때:
```bash
uv pip install --python .venv/bin/python -e ".[heic]"
```

설치 실패 시 (DSM glibc/wheel 호환 문제)는 그냥 넘겨도 됩니다 — exiftool이
HEIC 메타데이터/썸네일을 대신 처리합니다.

### 5) DB 스키마 생성

```bash
.venv/bin/python -m alembic upgrade head
```

`data/catalog.db` (SQLite)가 생성되고 모든 테이블이 만들어집니다. 처음에는
`0001` ~ 가장 최신 마이그레이션까지 순서대로 적용됩니다.

검증:
```bash
.venv/bin/python -m alembic current
# 출력 끝줄에 (head)가 있어야 OK — 예: 0005_tags_description (head)
```

> **업데이트할 때마다 이 단계 한 번 더 실행**하는 게 안전합니다. 새 컬럼이나
> 테이블 추가가 있었다면 자동 반영되고, 없으면 no-op.

### 6) (선택) 호스트별 설정

대부분의 값은 설치 후 관리 UI에서 변경 가능합니다 (관리 → 설정 탭).
지금 손댈 게 거의 없습니다만, 미리 바꾸고 싶다면:

```bash
[ -f config/local.toml ] || cp config/local.example.toml config/local.toml
# 편집기로 열어 수정 — 예시: 워커 동시성, 앱 이름, 시간대 등
```

`secret_key`는 첫 부팅 시 `data/session.secret`에 자동 생성됩니다.

### 7) systemd 서비스 등록

```bash
./scripts/install-systemd.sh
```

스크립트가 현재 사용자 (`$USER`)와 설치 경로 (`$PWD`)를 자동으로 채워서
두 unit 파일을 `/etc/systemd/system/`에 설치합니다:
- `myphotos-api.service` — FastAPI (uvicorn) 8888 포트
- `myphotos-worker.service` — 스캐너 + 색인 워커

```bash
sudo systemctl enable myphotos-api myphotos-worker
sudo systemctl start  myphotos-api myphotos-worker
```

> DSM의 옛 systemd 빌드는 `--now` 옵션을 지원 안 해서 `enable`과 `start`를
> 두 줄로 분리했습니다.

검증:
```bash
sudo systemctl status myphotos-api    | head -3
sudo systemctl status myphotos-worker | head -3
# 둘 다 "Active: active (running)" 이어야 OK
```

### 8) 첫 로그인 & 사진 폴더 등록

1. 브라우저에서 `http://<NAS-IP>:8888` 접속 (예: `http://192.168.1.10:8888`)
2. **admin / admin** 로그인
3. 빨간 띠의 "지금 변경" 클릭 → 새 비밀번호 설정 (4자 이상)
4. 우상단 **관리** → **사진 폴더** 탭 → **새 폴더 추가**:
   - **라벨**: `family` (영숫자/`_`/`-`만)
   - **절대 경로**: 실제 사진 폴더 (예: `/volume1/photo`)
   - **읽기 전용**: 체크 권장 (스캐너가 원본 파일을 만지지 않음)
5. 추가된 행에서 **시험** 버튼 클릭 → 200장 샘플 색인이 큐에 등록됨
6. **색인** 탭에서 진행 상황 확인 (5초마다 자동 갱신). 실패한 잡이 0건이면
7. 다시 **사진 폴더** 탭 → 같은 행의 limit 입력은 비우고 **스캔** 버튼 → 풀스캔 시작
   - 10만 장 기준 NAS HDD에서 6~12시간 정도 소요

### 9) (선택) 가족 사용자 추가

관리 → **사용자** 탭 → **새 사용자 추가**:
- 사용자명: `mom`, `dad` 등
- 비밀번호: 임의 설정
- 관리자 권한: 보통 X (보기·공유·태그·코멘트 가능, 삭제는 불가)

### 10) (선택) 외부 노출

기본은 LAN 전체 (`0.0.0.0:8888`). WAN에서 쓰려면:
- DSM 제어판의 **역방향 프록시** 룰로 HTTPS 도메인 → `localhost:8888`
- 또는 [Tailscale](https://tailscale.com) 등 VPN 메시

자체 세션 쿠키 인증이라 외부 LB가 그대로 통과해도 무방.

## 설치 후 운영

### 코드 업데이트

```bash
cd ~/myphotos && git pull
uv pip install --python .venv/bin/python -e .       # 의존성 변경 시
.venv/bin/python -m alembic upgrade head            # 스키마 변경 시
sudo systemctl restart myphotos-api myphotos-worker
```

> 어떤 단계가 필요한지 헷갈리면 그냥 4줄 다 실행해도 안전합니다 — 변경
> 없으면 모두 no-op.

### 포트 변경

`config/local.toml`에:
```toml
[server]
port = 9000
```
그 후 `sudo systemctl restart myphotos-api`. 그리고 `myphotos-api.service`의
ExecStart에 포트가 박혀 있다면 `./scripts/install-systemd.sh` 재실행.

### 로그 보기
```bash
sudo journalctl -u myphotos-api    -n 60 --no-pager
sudo journalctl -u myphotos-worker -f
```

### 문제 해결

| 증상 | 확인 / 해결 |
| --- | --- |
| 타임라인이 비거나 500 오류 | `alembic current`가 `(head)`인지 확인. 아니면 `alembic upgrade head` 후 재시작 |
| 색인이 너무 느림 | 관리 → 설정 → 워커 → `concurrency` 조정. HDD면 3~4가 더 빠를 수 있음 |
| 워커 좀비 (status에 두 개 떠 있음) | `ps -ef \| grep app.worker`로 확인 후 systemd 외부 프로세스 `kill` |
| admin 비밀번호 잊음 | `.venv/bin/python -c "from app.auth import hash_password; print(hash_password('새비번'))"` → 출력 해시를 sqlite3로 `UPDATE users SET password_hash='<해시>' WHERE username='admin';` |

## 부트스트랩 (Windows 개발 환경)

```powershell
cd $env:USERPROFILE
git clone https://github.com/saintsc-ai/MyPhotos.git myphotos
cd myphotos
.\scripts\bootstrap.ps1
Copy-Item config\local.example.toml config\local.toml -ErrorAction SilentlyContinue
.\.venv\Scripts\python -m alembic upgrade head
.\scripts\run-api.ps1     # 한 터미널
.\scripts\run-worker.ps1  # 다른 터미널
```

→ `http://localhost:8888` 접속, admin/admin 로그인.

## 다른 호스트로 이전 (재인덱싱 없이)

다른 NAS로 이전해도 **재인덱싱 없이** 그대로 사용 가능합니다. 썸네일은
SHA-256으로 주소되고, `photos.rel_path`는 root 기준 상대 경로(POSIX/NFC)로
저장되어 있어 호스트별로 바뀌는 건 `roots.abs_path` 하나뿐입니다.

### 1) 원본 호스트 — 정합성 있는 스냅샷

```bash
sudo systemctl stop myphotos-api myphotos-worker
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
- **Worker**: separate process for scanning, EXIF extraction, thumbnail generation
- **Storage**: indexes existing folders read-only; thumbnails and DB live inside `data/`
- **Target host**: Synology DSM (DS3622xs+, x86_64) via systemd

## Layout

```text
myphotos/
├── app/                # application code
│   ├── api/            # FastAPI app (uvicorn entry)
│   ├── admin/          # admin CRUD (roots, jobs)
│   ├── worker/         # scanner + job runner (systemd entry)
│   └── web/            # HTMX templates / static
├── config/
│   ├── default.toml    # built-in defaults (tracked)
│   └── local.toml      # per-host overrides (NOT tracked)
├── data/               # runtime (NOT tracked) — DB, thumbs, logs, trash
├── vendor/             # OS-specific binaries (exiftool, ffmpeg)
├── alembic/            # DB migrations
├── scripts/            # bootstrap, systemd install
└── systemd/            # unit templates
```

## Install (Synology NAS — step by step)

> All commands use `~` (current user's home) and `$USER` (current user's
> name), so they work for any DSM account — no need to substitute a
> username. The DSM home directory is normally
> `/var/services/homes/$USER`, which `~` resolves to automatically.
>
> The install folder name (`myphotos` below) is also arbitrary — use a
> different name if you prefer; just replace `~/myphotos` accordingly.

### Prerequisites

| Item | Notes |
| --- | --- |
| DSM user account | Any login; needs `sudo` for systemd unit install |
| SSH access | DSM Control Panel → Terminal & SNMP → enable SSH |
| Internet | for uv / dependencies / vendor binary downloads |
| Photo root folder | e.g. `/volume1/photo`, readable by the user |
| Port 8888 free | otherwise see [post-install](#post-install) |

### 0) Install uv (one time)

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
source ~/.bashrc
uv python install 3.11.9
```

### 1) Clone the repo

```bash
cd ~
git clone https://github.com/saintsc-ai/MyPhotos.git myphotos
cd ~/myphotos
```

### 2) Python venv + dependencies

```bash
./scripts/bootstrap.sh
```

### 3) exiftool / ffmpeg (RAW / HEIC / video thumbnails)

```bash
./scripts/install-vendor-linux-x64.sh
```

### 4) (optional) Native HEIC reader

```bash
uv pip install --python .venv/bin/python -e ".[heic]"
```

Skip silently on glibc/wheel mismatch — exiftool will handle HEIC.

### 5) Create / upgrade the DB schema

```bash
.venv/bin/python -m alembic upgrade head
```

Verify with `.venv/bin/python -m alembic current` — should end with
`(head)`. **Re-run this step on every code update** to pick up new
columns/tables; it's a no-op when nothing changed.

### 6) (optional) Host overrides

Most settings are editable later in the admin UI (관리 → 설정). If you
want to seed values up-front:

```bash
[ -f config/local.toml ] || cp config/local.example.toml config/local.toml
```

`secret_key` is auto-generated to `data/session.secret` on first boot.

### 7) Install systemd units

```bash
./scripts/install-systemd.sh        # fills $USER + $PWD into the templates
sudo systemctl enable myphotos-api myphotos-worker
sudo systemctl start  myphotos-api myphotos-worker
```

> DSM ships an older systemd that doesn't accept `--now`, so `enable`
> and `start` are split.

### 8) First login + photo root

1. Open `http://<NAS-IP>:8888`
2. Sign in with **admin / admin**
3. Use the "지금 변경" prompt to set a real password (≥ 4 chars)
4. Top-right **관리 (Admin)** → **사진 폴더 (Roots)** → **새 폴더 추가**:
   - Label: `family` (alphanumerics, `_`, `-`)
   - Absolute path: your photo folder (e.g. `/volume1/photo`)
   - Read-only: checked (recommended; scanner won't touch originals)
5. On the new row, **시험 (Sample, 200 photos)** for a smoke test
6. Watch **색인 (Indexing)** tab for progress (auto-refreshes every 5s)
7. Back to **사진 폴더**, **스캔 (Scan)** with no limit for a full run

### 9) (optional) Add family users

관리 → **사용자 (Users)** → 새 사용자 추가. Leave "관리자" unchecked for
non-admin accounts that can browse / share / tag / comment but not delete.

### 10) (optional) Expose externally

DSM Reverse Proxy → `localhost:8888`, or wrap the host with Tailscale.
Session cookies pass through any standard LB.

> Note: uv-created venvs don't include `pip`. Use `uv pip install ...` for
> ad-hoc installs, or `.venv/bin/python -m <module>` to run scripts.

## Post-install

### Updating the code

```bash
cd ~/myphotos && git pull
uv pip install --python .venv/bin/python -e .       # if deps changed
.venv/bin/python -m alembic upgrade head            # if schema changed
sudo systemctl restart myphotos-api myphotos-worker
```

Running all four lines is always safe — they no-op when nothing changed.

### Troubleshooting

| Symptom | Check / fix |
| --- | --- |
| Empty timeline or 500 errors | `alembic current` should end in `(head)`; if not, `alembic upgrade head` and restart |
| Slow indexing | 관리 → 설정 → worker → `concurrency`. HDD storage often goes faster at 3–4 than 6+ |
| Two worker processes (status shows it) | `ps -ef \| grep app.worker`; `kill` any not under systemd |
| Forgot admin password | `.venv/bin/python -c "from app.auth import hash_password; print(hash_password('new_pw'))"`, then `sqlite3 data/catalog.db "UPDATE users SET password_hash='<hash>' WHERE username='admin';"` |

## Bootstrap (Windows dev)

```powershell
.\scripts\bootstrap.ps1
Copy-Item config\local.example.toml config\local.toml
.\scripts\run-api.ps1     # in one terminal
.\scripts\run-worker.ps1  # in another
```

## Porting to a new host (without re-indexing)

Moving the catalog to a different NAS keeps every photo's index intact —
**no re-indexing required**. Thumbnails are addressed by SHA-256 and
`photos.rel_path` is stored as a POSIX/NFC path relative to the root,
so the only host-specific value is `roots.abs_path`.

### 1) Source host — consistent snapshot

```bash
sudo systemctl stop myphotos-api myphotos-worker
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

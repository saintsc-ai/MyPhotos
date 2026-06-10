# macOS 설치 가이드 (개발용)

> 한국어 / [English](#english)

> [← README로 돌아가기](../../README.md)

macOS에서 MyPhotos를 굴리는 가장 보편적인 시나리오는 **개발 환경**입니다 —
NAS에 푸시하기 전 로컬에서 코드 변경을 시험하거나 새 기능을 작업할 때.
운영용으로 24/7 굴리고 싶다면 [Docker 가이드](docker.md)(Docker Desktop)나
[Synology 가이드](synology.md)를 권장합니다.

서버를 터미널로 직접 띄워도 되고, **[데스크톱 앱](#데스크톱-앱)**으로
버튼 클릭만으로 시작/정지/모니터링해도 됩니다.

## 사전 준비

| 항목 | 비고 |
| --- | --- |
| macOS 12+ (Intel / Apple Silicon) | |
| **Git** | `git --version` — 없으면 `xcode-select --install` |
| **uv** | Python 3.11을 자동 설치/관리. 아래 부트스트랩이 사용 |
| **Homebrew** | exiftool / ffmpeg 설치용 ([brew.sh](https://brew.sh)) |
| 사진 폴더 | 예: `~/Pictures`. 읽기 권한 |

uv가 없으면:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
# 새 셸을 열거나:  source $HOME/.local/bin/env
```

## 부트스트랩 (한 번에)

```bash
git clone https://github.com/saintsc-ai/MyPhotos.git myphotos
cd myphotos
bash scripts/bootstrap.sh
```

`bootstrap.sh`가 자동으로:

- uv로 Python 3.11 가상환경 생성 (`.venv/`)
- `pyproject.toml` 의존성 설치 (editable)
- DB 마이그레이션 (`alembic upgrade head`)
- `config/local.toml` 생성

생성 후 `config/local.toml`의 `secret_key`를 강한 랜덤 값으로 바꾸세요:

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(48))"
```

### exiftool / ffmpeg (RAW · HEIC · 동영상 썸네일용)

```bash
brew install exiftool ffmpeg
```

`app/external.py`가 PATH(`/opt/homebrew/bin` 등)를 자동 감지하므로 추가
설정은 필요 없습니다.

### 선택 기능 (extras)

```bash
# HEIC, OCR, 외부 DB 드라이버 등 — 필요한 것만:
uv pip install --python .venv/bin/python -e ".[heic,ocr]"
```

| extra | 용도 |
| --- | --- |
| `heic` | HEIC/HEIF 디코딩 (아이폰 사진) |
| `ocr` | 사진 속 글자 검색 (RapidOCR, 한국어 모델 자동 다운로드) |
| `exif-extra` | 보조 EXIF 추출기 |
| `mariadb` / `postgres` | 외부 DB 드라이버 ([external-db.md](../operations/external-db.md)) |

### ML 모델 (자동 분류 — 선택)

객체검출 / CLIP / 얼굴 기능을 쓰려면 ONNX 모델을 받습니다:

```bash
bash scripts/install-ml-models.sh    # data/models/ 에 저장
```

## 실행

터미널 3개(또는 `&` 백그라운드)로:

```bash
# Web/API
.venv/bin/python -m uvicorn app.api.main:app --host 127.0.0.1 --port 8888
# 인덱싱 워커 (스캔/EXIF/썸네일)
.venv/bin/python -m app.worker.main
# ML 워커 (객체/CLIP/얼굴/OCR) — 선택
.venv/bin/python -m app.worker_ml.main
```

브라우저에서 http://127.0.0.1:8888 → 최초 로그인 `admin / admin`
(첫 로그인 후 비밀번호 변경 권장). 사진 폴더(root)는 **관리 → 사진 폴더**에서
등록하면 워커가 자동으로 색인합니다.

## 데스크톱 앱

터미널 대신 **데스크톱 앱** 하나로 서버를 관리할 수 있습니다 — 위 3개
프로세스를 버튼으로 시작/정지/재시작, 진행 상태·로그 확인, 트레이 상주.

```bash
cd desktop
uv venv --python 3.11 .venv        # 데스크톱 전용 venv (PySide6)
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python app.py
```

→ **서버 관리** 화면에서 `▶ 전체 시작`. 자세한 사용/빌드(`.app`)는
[desktop/README.md](../../desktop/README.md) 참고.

## 서버로 24/7 운영 (launchd)

데스크톱 앱 없이 mac을 항상 켜진 서버로 굴리려면 **launchd**로 세 프로세스를
서비스로 등록합니다. 로그인 시 자동 시작 + 죽으면 자동 재시작(`KeepAlive`).

사용자 로그인 세션에서 도는 **LaunchAgent**(`~/Library/LaunchAgents/`)가 가장
간단합니다. 아래는 인덱싱 워커 예시 — 경로(`/Users/me/myphotos`)를 본인
것으로 바꾸세요:

`~/Library/LaunchAgents/com.myphotos.worker.plist`

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>             <string>com.myphotos.worker</string>
  <key>ProgramArguments</key>
  <array>
    <string>/Users/me/myphotos/.venv/bin/python</string>
    <string>-m</string><string>app.worker.main</string>
  </array>
  <key>WorkingDirectory</key>  <string>/Users/me/myphotos</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key><string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin</string>
    <key>PYTHONUNBUFFERED</key><string>1</string>
  </dict>
  <key>RunAtLoad</key>         <true/>
  <key>KeepAlive</key>         <true/>
  <key>StandardOutPath</key>   <string>/Users/me/myphotos/data/logs/worker.out.log</string>
  <key>StandardErrorPath</key> <string>/Users/me/myphotos/data/logs/worker.err.log</string>
</dict>
</plist>
```

나머지 둘은 `ProgramArguments`만 다르게 해서 같은 형식으로 만듭니다:

| 서비스 | Label | ProgramArguments (python 뒤) |
| --- | --- | --- |
| Web/API | `com.myphotos.api` | `-m uvicorn app.api.main:app --host 0.0.0.0 --port 8888` |
| 인덱싱 워커 | `com.myphotos.worker` | `-m app.worker.main` |
| ML 워커 | `com.myphotos.ml` | `-m app.worker_ml.main` |

> `--host 0.0.0.0`은 LAN에서 접속하는 경우. 리버스 프록시(Caddy 등) 뒤에
> 둔다면 `127.0.0.1`로.

등록 / 시작 (각 plist에 대해):

```bash
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.myphotos.api.plist
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.myphotos.worker.plist
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.myphotos.ml.plist
```

확인 / 중지 / 재시작 / 해제:

```bash
launchctl print gui/$(id -u)/com.myphotos.worker     # 상태
launchctl kickstart -k gui/$(id -u)/com.myphotos.worker   # 재시작
launchctl bootout gui/$(id -u)/com.myphotos.worker   # 중지·해제
```

> **로그아웃 중에도 돌려야 하면**(헤드리스 부팅) LaunchAgent 대신
> `/Library/LaunchDaemons/`에 두고 `sudo launchctl bootstrap system …`으로
> 올립니다. 이땐 root로 실행되니 사진/`data/` 폴더 권한에 주의하고, plist에
> `<key>UserName</key><string>me</string>`로 실행 사용자를 지정하세요.

## 색인 데이터 위치

`<프로젝트 폴더>/data/` (catalog.db · 썸네일 · 모델 · 휴지통). `data/`는
gitignore 대상이라 커밋되지 않습니다. 다른 디스크로 옮기려면
`config/local.toml`의 `[paths] data_dir`를 지정하세요.

---

## English

The usual way to run MyPhotos on macOS is a **dev environment** — testing
local changes before pushing to a NAS. For 24/7 production use the
[Docker guide](docker.md) (Docker Desktop) or [Synology guide](synology.md).

Run the server from a terminal, or manage it from the
**[desktop app](#desktop-app)** (start/stop/monitor with buttons).

## Prerequisites

| Item | Notes |
| --- | --- |
| macOS 12+ (Intel / Apple Silicon) | |
| **Git** | `git --version` — install via `xcode-select --install` |
| **uv** | auto-installs/manages Python 3.11; used by bootstrap |
| **Homebrew** | for exiftool / ffmpeg ([brew.sh](https://brew.sh)) |
| Photo folder | e.g. `~/Pictures`, readable |

Install uv if missing:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
source $HOME/.local/bin/env
```

## Bootstrap (one shot)

```bash
git clone https://github.com/saintsc-ai/MyPhotos.git myphotos
cd myphotos
bash scripts/bootstrap.sh
```

`bootstrap.sh` creates the `.venv/` (Python 3.11 via uv), installs deps,
runs `alembic upgrade head`, and writes `config/local.toml`. Replace its
`secret_key` with a strong random string before first run.

### exiftool / ffmpeg

```bash
brew install exiftool ffmpeg     # auto-detected from PATH
```

### Optional extras

```bash
uv pip install --python .venv/bin/python -e ".[heic,ocr]"
```

`heic` (iPhone HEIC), `ocr` (text-in-photo search), `exif-extra`,
`mariadb` / `postgres` (external DB).

### ML models (optional auto-classification)

```bash
bash scripts/install-ml-models.sh
```

## Run

```bash
.venv/bin/python -m uvicorn app.api.main:app --host 127.0.0.1 --port 8888
.venv/bin/python -m app.worker.main
.venv/bin/python -m app.worker_ml.main   # optional
```

Open http://127.0.0.1:8888, log in with `admin / admin`, then add a photo
root under **Admin → Photo folders**.

## Desktop app

Manage the server from one desktop app instead of a terminal —
start/stop/restart the three processes, watch progress and logs, stays in
the tray:

```bash
cd desktop
uv venv --python 3.11 .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python app.py
```

See [desktop/README.md](../../desktop/README.md) for details and the
`.app` build.

## Run as a 24/7 service (launchd)

To keep an always-on Mac serving without the desktop app, register the
three processes with **launchd** — auto-start at login + auto-restart on
crash (`KeepAlive`). A per-user **LaunchAgent** (`~/Library/LaunchAgents/`)
is simplest. Worker example (replace `/Users/me/myphotos`):

`~/Library/LaunchAgents/com.myphotos.worker.plist`

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>             <string>com.myphotos.worker</string>
  <key>ProgramArguments</key>
  <array>
    <string>/Users/me/myphotos/.venv/bin/python</string>
    <string>-m</string><string>app.worker.main</string>
  </array>
  <key>WorkingDirectory</key>  <string>/Users/me/myphotos</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key><string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin</string>
    <key>PYTHONUNBUFFERED</key><string>1</string>
  </dict>
  <key>RunAtLoad</key>         <true/>
  <key>KeepAlive</key>         <true/>
  <key>StandardOutPath</key>   <string>/Users/me/myphotos/data/logs/worker.out.log</string>
  <key>StandardErrorPath</key> <string>/Users/me/myphotos/data/logs/worker.err.log</string>
</dict>
</plist>
```

Make the other two the same way, differing only in `ProgramArguments`:

| Service | Label | ProgramArguments (after python) |
| --- | --- | --- |
| Web/API | `com.myphotos.api` | `-m uvicorn app.api.main:app --host 0.0.0.0 --port 8888` |
| Indexing worker | `com.myphotos.worker` | `-m app.worker.main` |
| ML worker | `com.myphotos.ml` | `-m app.worker_ml.main` |

Load / start, inspect, restart, unload:

```bash
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.myphotos.api.plist
launchctl print     gui/$(id -u)/com.myphotos.worker         # status
launchctl kickstart -k gui/$(id -u)/com.myphotos.worker      # restart
launchctl bootout   gui/$(id -u)/com.myphotos.worker         # stop + unload
```

> To run while logged out (headless boot), put the plists in
> `/Library/LaunchDaemons/` and `sudo launchctl bootstrap system …`
> instead. They then run as root, so mind photo/`data/` permissions and
> pin the user with `<key>UserName</key><string>me</string>`.

## Where the index lives

`<project>/data/` (catalog.db, thumbnails, models, trash). `data/` is
gitignored. To relocate, set `[paths] data_dir` in `config/local.toml`.

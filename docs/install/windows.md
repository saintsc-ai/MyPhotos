# Windows 설치 가이드 (개발용)

> 한국어 / [English](#english)

> [← README로 돌아가기](../../README.md)

Windows에서 MyPhotos를 굴리는 가장 보편적인 시나리오는 **개발 환경**입니다 —
NAS에 푸시하기 전 로컬에서 코드 변경을 시험하거나 새 기능을 작업할 때.
운영용으로 Windows 서버에서 24/7 굴리고 싶다면 [Docker 가이드](docker.md)를
권장합니다 (Docker Desktop on Windows).

## 사전 준비

| 항목 | 비고 |
| --- | --- |
| Windows 10/11 | PowerShell 5.1+ 또는 PowerShell 7 |
| **Git for Windows** | [git-scm.com](https://git-scm.com) — `git --version` 검증 |
| **Python 3.11.x** | uv가 자동으로 설치하므로 따로 안 받아도 OK. 시스템에 이미 있으면 그것 사용 |
| 사진 폴더 | 예: `D:\Photos`. 읽기 권한 |

## 부트스트랩 (한 번에)

PowerShell에서:

```powershell
cd $env:USERPROFILE
git clone https://github.com/saintsc-ai/MyPhotos.git myphotos
cd myphotos
.\scripts\bootstrap.ps1
Copy-Item config\local.example.toml config\local.toml -ErrorAction SilentlyContinue
.\.venv\Scripts\python -m alembic upgrade head
```

`bootstrap.ps1`이 자동으로 처리:
- uv 설치 (없으면)
- Python 3.11+ 가상환경 (`.venv\`)
- `pyproject.toml` 의존성 설치

> **`onnxruntime` / `numpy` / `tokenizers` 휠 해석 에러가 나면** —
> 기본 핀은 모든 플랫폼에서 가장 넓게 잡혀있지만 (`onnxruntime>=1.16`
> 등), 정말 드물게 본인 Python 버전에 맞는 wheel이 없을 수 있습니다.
> 그땐 시스템 Python 버전을 확인 (`python --version`) — Python 3.13이
> 너무 새것일 가능성이 있으면 `py -3.11` 또는 `py -3.12` 로 명시:
>
> ```powershell
> $env:PYTHON_BIN = "py -3.12"
> .\scripts\bootstrap.ps1
> ```

### exiftool / ffmpeg (RAW · HEIC · 동영상 썸네일용)

bootstrap.ps1은 두 바이너리를 받아주지 않습니다 (`install-vendor-*`는
현재 Linux x64만 제공). 두 옵션 중 하나:

**A) Scoop로 시스템에 설치** (가장 단순, 관리자 권한 불필요)

Scoop이 없으면 먼저 PowerShell(일반)에서:

```powershell
Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope CurrentUser
irm get.scoop.sh | iex
```

이미 깔려있거나 위로 설치 끝났으면:

```powershell
scoop install exiftool ffmpeg
```

검증:

```powershell
exiftool -ver
ffmpeg -version | Select-Object -First 1
```

`app/external.py`가 PATH를 자동 감지하므로 추가 설정 불필요.

> **Chocolatey가 이미 있으면**: 관리자 PowerShell에서
> `choco install exiftool ffmpeg`로도 동일. 검증 단계는 같습니다.

**B) 수동 다운로드 → `vendor\windows-x64\`** (패키지 매니저 안 쓰고 싶을 때)

폴더 먼저 만들기 (없으면):

```powershell
New-Item -ItemType Directory -Force -Path $env:USERPROFILE\myphotos\vendor\windows-x64
```

브라우저로:

- **ExifTool**: <https://exiftool.org> → "Windows Executable" zip 다운로드 →
  압축 풀고 `exiftool(-k).exe` → 이름을 `exiftool.exe`로 변경 →
  `vendor\windows-x64\` 에 복사
- **FFmpeg**: <https://www.gyan.dev/ffmpeg/builds/> →
  "release essentials" zip → 압축 풀고 `bin\ffmpeg.exe` → 같은 폴더에 복사

검증:

```powershell
& $env:USERPROFILE\myphotos\vendor\windows-x64\exiftool.exe -ver
& $env:USERPROFILE\myphotos\vendor\windows-x64\ffmpeg.exe -version | Select-Object -First 1
```

설치 안 하면 인덱싱 자체는 동작하지만 **HEIC / RAW / 동영상 썸네일이 모두
실패 상태로 남습니다** — 관리 → 색인 진행에서 `thumb_status=failed` 카운트가
높으면 이 단계가 안 끝난 신호. 깔고 나서 워커 재시작 (`run-worker.ps1`
Ctrl+C → 재실행) + 관리 → 색인 → 실패 잡 재시도.

### (선택) Pillow의 네이티브 HEIC 디코더

ExifTool로도 HEIC 처리가 되지만, Pillow가 직접 디코드하면 더 빠릅니다
(iPhone HEIC 라이브러리가 크면 체감 차이가 큽니다):

```powershell
.\.venv\Scripts\python -m pip install -e ".[heic]"
```

Windows용 `pillow-heif` wheel은 libheif를 정적 번들로 들고 와서 별도 시스템
패키지가 필요 없습니다. 설치 실패 시는 그냥 넘어가도 됨 — ExifTool fallback이
HEIC 메타데이터/썸네일을 대신 처리합니다.

> ⚠ **순서 중요** — 위 셋(exiftool / ffmpeg / pillow-heif) 모두 **워커
> 띄우기 전에** 설치하세요. 워커가 부팅 시 외부 바이너리 / 옵션 라이브러리
> 가용성을 한 번 캐싱합니다. 부팅 시점에 없던 게 나중에 깔리면 워커는
> 재시작 전까지 모르고 계속 "없다"고 판단해 HEIC / 동영상 / RAW가 모두
> `failed`로 마킹됩니다. (코드는 positive-only 캐싱으로 개선됐지만,
> 그래도 처음부터 깔고 시작하는 게 안전.)

## 실행

API와 워커는 **별도 터미널 두 개**에서 띄웁니다:

```powershell
.\scripts\run-api.ps1     # 한 터미널
```

```powershell
.\scripts\run-worker.ps1  # 다른 터미널
```

→ 브라우저에서 `http://localhost:8888` → **admin / admin** 로그인.

### ML 자동 분류 (선택)

**1) 모델 다운로드 (한 번만)** — Bash 스크립트이므로 **Git Bash**
(`MINGW64`)에서 실행하세요. PowerShell에선 `bash`로 호출해도 인자의
백슬래시가 escape로 먹혀 경로가 깨집니다.

Git Bash에서:

```bash
./scripts/install-ml-models.sh
```

PowerShell에서 굳이 돌리고 싶다면 슬래시 경로로 명시:

```powershell
bash -c "./scripts/install-ml-models.sh"
```

bash 자체가 없으면 [Release 페이지](https://github.com/saintsc-ai/MyPhotos/releases)
에서 6개 ONNX 파일(~140MB)을 직접 받아 `data\models\` 아래에 놓아도 됩니다.

**2) ML 워커 실행** — 세 번째 PowerShell 터미널에서:

```powershell
.\scripts\run-ml-worker.ps1
```

모델이 없으면 워커가 뜨자마자 "model missing" 로그 후 idle 상태로 머뭅니다 —
1)을 먼저 끝내고 켜세요.

## 사진 폴더 등록

1. `http://localhost:8888/admin.html` 접속
2. **사진 폴더** 탭 → **새 폴더 추가**
3. 절대 경로 입력 — Windows는 `D:\Photos` 형식 그대로 (백슬래시)
4. **시험** 버튼 → 200장 샘플 → **색인** 탭에서 진행 확인

> Windows에서 `taken_at` / GPS EXIF 편집과 회전 기능은 정상 동작하지만,
> NTFS 권한 모델이 POSIX와 달라 일부 케이스에서 `Permission denied`가 다른
> 형태로 나타날 수 있습니다. 사진 폴더가 OneDrive / Dropbox 동기화 대상이면
> 동기화 파일 잠금과 충돌할 수 있으니 주의.

## 서비스 통합 관리 (status / start / stop / restart)

3개를 매번 별도 터미널에서 Ctrl+C → 재실행하기 번거롭다면 `myphotos.ps1`
한 줄로 일괄 관리 가능 (systemctl 비슷한 API):

```powershell
.\scripts\myphotos.ps1 status     # 어떤 게 살아있나, PID, uptime
.\scripts\myphotos.ps1 start      # 안 떠있는 것만 새 minimised 창으로 기동
.\scripts\myphotos.ps1 stop       # 좀비 포함 전부 종료 (Get-CimInstance 매칭)
.\scripts\myphotos.ps1 restart    # stop + start
```

각 서비스는 별도 최소화된 PowerShell 창으로 떠서 로그를 보고 싶을 때
작업 표시줄에서 클릭. `stop`은 command-line 패턴 매칭으로 PID를 잡아서
**옛 터미널 닫고 새 터미널 띄울 때 발생하는 좀비 워커도 같이 정리**합니다
(이게 정확히 색인 실패의 흔한 원인 — 위 [트러블슈팅](#트러블슈팅) 참고).

운영용 백그라운드 서비스로 굳히려면 아래 [Docker 권장](#운영용으로는-docker-권장)
섹션 참고.

## 데스크톱 앱 (선택)

브라우저 + `myphotos.ps1` 대신, **데스크톱 앱** 하나로 갤러리 보기와 서버
관리를 함께 할 수 있습니다 — Web/API · 인덱싱 워커 · ML 워커를 버튼으로
시작/정지/재시작, 라이브 로그, 인덱싱 진행 상태까지. 최소화하면 트레이에
상주해 워커가 계속 돕니다.

PowerShell에서 (데스크톱 전용 venv — PySide6):

```powershell
cd desktop
python -m venv .venv
.\.venv\Scripts\python -m pip install -r requirements.txt
.\.venv\Scripts\python app.py
```

→ **서버 관리** 화면에서 `▶ 전체 시작`. 워커는 프로젝트 쪽 `.venv`의
python으로 자동 실행됩니다(자동 감지, 필요 시 "경로 설정"에서 변경). 단일
`MyPhotos.exe` 빌드와 자세한 사용법은
[desktop/README.md](../../desktop/README.md) 참고.

## 코드 변경 → 재시작

API와 워커는 코드 변경 시 자동 재시작 안 됩니다 (uvicorn `--reload`는
인덱싱 워커에 영향이 큼). 가장 빠르게:

```powershell
.\scripts\myphotos.ps1 restart
```

또는 각 터미널에서 개별 Ctrl+C → `run-*.ps1` 재실행 (기존 방식).

## 트러블슈팅

Windows에서 자주 만나는 패턴 + 빠른 진단/회복.

### 색인이 계속 실패 — "ffmpeg / exiftool not available" 또는 HEIC "cannot identify image file"

워커 부팅 후 ExifTool / FFmpeg / pillow-heif를 깐 경우, 해당 워커
프로세스는 캐시된 "없음"을 들고 있어 계속 실패합니다.

**1) 정말 워커 하나만 살아있는지 확인.** 새 PowerShell 터미널 열어:

```powershell
Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
  Where-Object { $_.CommandLine -match 'worker.main' } |
  Select-Object ProcessId, CreationDate, CommandLine | Format-List
```

`worker.main` 명령이 **두 쌍 이상**(한 쌍 = venv 런처 + 실제 인터프리터)
나오면 옛 워커가 좀비처럼 살아있는 것. Ctrl+C로 띄운 터미널을 닫아도
백그라운드 프로세스가 그대로 남아있는 경우가 있음. 그 PID들 강제 종료:

```powershell
Stop-Process -Id <PID1>,<PID2> -Force
```

**2) 새 워커 하나만 살린 상태에서 외부 바이너리 가용성 직접 확인.** Git
Bash 또는 PowerShell에서:

```powershell
.\.venv\Scripts\python.exe -c "from app import external; print('exiftool:', external.exiftool_path()); print('ffmpeg:  ', external.ffmpeg_path())"
```

둘 다 경로가 찍히면 OK. `None`이면 vendor 디렉토리 / scoop PATH를 다시
확인. 새 subprocess는 워커의 캐시와 무관하므로 진짜 가용성을 보여줌.

**3) 새 워커가 정말로 새 바이너리를 보는지 확인.** 워커 띄운 직후 그
워커 콘솔 첫 한두 줄에 이런 로그가 떠야 함:

```text
INFO app.external: exiftool: C:\Users\scsung\myphotos\vendor\windows-x64\exiftool.exe
INFO app.external: ffmpeg: C:\Users\scsung\myphotos\vendor\windows-x64\ffmpeg.exe
```

이 줄이 안 보이고 첫 사진 처리에서 "not available"이 나오면 워커는
아직 옛 바이너리(or 없음) 기준으로 동작 중 — Ctrl+C → 재실행.

**4) 옛 실패 카운트 청산.** 위가 다 OK인데 색인 화면의 실패 카운트가
줄어들지 않는다면 → 옛 실패 기록일 뿐. 관리 → 색인 → **재색인** 버튼
(failed + partial 모두 체크) 누르면 새 워커가 재처리해서 카운트 갱신.

### scoop으로 깐 바이너리가 워커에 안 잡힘

scoop은 사용자 PATH에 `~/scoop/shims/`를 추가하는데, **이미 열려있던
PowerShell 터미널**은 옛 PATH를 그대로 갖습니다. scoop 설치 후엔
새 PowerShell 창에서 `run-worker.ps1`을 띄워야 함.

또는 vendor 우선 정책을 활용 — `vendor\windows-x64\` 폴더에 두 exe를
한 번 복사해 두면 PATH 변동과 무관하게 항상 잡힘 (vendor가 PATH보다
우선).

확인:

```powershell
($env:PATH -split ';') | Select-String scoop
```

비어있으면 그 터미널은 scoop을 모르는 상태.

### HEIC 만 실패 — "pillow: cannot identify image file"

`pillow-heif`가 워커 부팅 후에 설치됐거나, `.[heic]` 옵션 설치를
빼먹은 케이스.

```powershell
# 설치 (이미 했으면 no-op)
.\.venv\Scripts\python -m pip install -e ".[heic]"

# 워커 재시작 — 모듈 가져온 시점에 register_heif_opener()가 실행됨
# (위 트러블슈팅 1번처럼 옛 워커 잔존 확인 + 종료)
.\scripts\run-worker.ps1
```

확인:

```powershell
.\.venv\Scripts\python.exe -c "import pillow_heif; print('heif version:', pillow_heif.__version__)"
```

버전이 찍히면 패키지는 정상. 그래도 HEIC가 실패하면 워커가 옛 프로세스인지 트러블슈팅 1번을 다시 확인.

### 휴지통 사진이 사라짐 — 휴지통 UI는 비어있는데 `data\trash\` 폴더는 가득

`status='trashed'` → `status='missing'`으로 잘못 전환된 옛 버그
(2026-05-31에 수정됨, 커밋 `e36ccdd`). 회복 엔드포인트가 있음 — F12
콘솔에서:

```javascript
fetch('/api/admin/trash/repair', { method: 'POST' }).then(r => r.json()).then(console.log)
```

`{repaired: N, inspected: M}` 출력 후 휴지통 새로고침 → 사라졌던
사진들 다시 표시 → 정상 복구 가능.

## 운영용으로는 Docker 권장

Windows에서 백그라운드 서비스로 굴리려면 systemd가 없어 다음 중 하나:

1. **Docker Desktop on Windows** — [Docker 가이드](docker.md) 그대로 적용
   가능. compose가 컨테이너를 자동 재시작하고 시스템 트레이에 상태 표시.
2. **WSL2 + systemd** — WSL2 Ubuntu에 [Linux 가이드](linux.md)대로 설치.
   Windows에선 보이지만 실제론 Linux 환경.
3. **Windows Task Scheduler** — `run-api.ps1` / `run-worker.ps1`을 부팅 시
   실행하는 작업으로 등록. systemd 수준의 프로세스 관리는 안 됨.

가족용 사진 카탈로그 운영이 목표라면 NAS(Synology / 일반 Linux) 또는
Docker 쪽이 훨씬 안정적입니다.

---

# English

## Windows install guide (dev)

> [← Back to README](../../README.md)

The typical Windows use case is **development** — testing changes
locally before pushing to the NAS, or working on new features. For
24/7 production on Windows, the [Docker guide](docker.md) with Docker
Desktop is the recommended path.

### Prerequisites

| Item | Notes |
| --- | --- |
| Windows 10/11 | PowerShell 5.1+ or PowerShell 7 |
| **Git for Windows** | [git-scm.com](https://git-scm.com); verify with `git --version` |
| **Python 3.11.x** | uv installs it automatically; system Python is reused if present |
| Photo folder | e.g. `D:\Photos`, readable |

### Bootstrap (one shot)

In PowerShell:

```powershell
cd $env:USERPROFILE
git clone https://github.com/saintsc-ai/MyPhotos.git myphotos
cd myphotos
.\scripts\bootstrap.ps1
Copy-Item config\local.example.toml config\local.toml -ErrorAction SilentlyContinue
.\.venv\Scripts\python -m alembic upgrade head
```

`bootstrap.ps1` handles:
- uv install (if absent)
- Python 3.11+ venv in `.venv\`
- `pyproject.toml` deps

### exiftool / ffmpeg (RAW · HEIC · video thumbnails)

bootstrap.ps1 doesn't fetch these (`install-vendor-*` ships only the
Linux x64 build today). Two options:

**A) Install via Scoop** (simplest, no admin needed)

If you don't have Scoop yet, install it from a regular PowerShell:

```powershell
Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope CurrentUser
irm get.scoop.sh | iex
```

Then in the same PowerShell:

```powershell
scoop install exiftool ffmpeg
```

Verify:

```powershell
exiftool -ver
ffmpeg -version | Select-Object -First 1
```

`app/external.py` auto-detects PATH, so no further config needed.

> **If you already have Chocolatey**: `choco install exiftool ffmpeg`
> from an admin PowerShell also works. Verification commands are the same.

**B) Manual download → `vendor\windows-x64\`** (no package manager)

Make the folder first if it doesn't exist:

```powershell
New-Item -ItemType Directory -Force -Path $env:USERPROFILE\myphotos\vendor\windows-x64
```

Then in your browser:

- **ExifTool**: <https://exiftool.org> → "Windows Executable" zip →
  unzip, rename `exiftool(-k).exe` to `exiftool.exe` →
  drop into `vendor\windows-x64\`
- **FFmpeg**: <https://www.gyan.dev/ffmpeg/builds/> →
  "release essentials" zip → unzip, copy `bin\ffmpeg.exe` into the same folder

Verify:

```powershell
& $env:USERPROFILE\myphotos\vendor\windows-x64\exiftool.exe -ver
& $env:USERPROFILE\myphotos\vendor\windows-x64\ffmpeg.exe -version | Select-Object -First 1
```

Skip this step and indexing itself still works, but **every HEIC / RAW /
video thumbnail fails** — a high `thumb_status=failed` count on the admin
indexing tab is the tell. After installing, restart the worker
(`run-worker.ps1` Ctrl+C → re-run) and use admin → 색인 → retry failed
jobs to backfill anything that failed earlier.

### (optional) Pillow's native HEIC decoder

ExifTool already handles HEIC, but if Pillow can decode it directly the
indexing is noticeably faster on big iPhone libraries:

```powershell
.\.venv\Scripts\python -m pip install -e ".[heic]"
```

The Windows `pillow-heif` wheel statically bundles libheif so no system
packages are needed. Skip silently on failure — ExifTool fallback covers
HEIC metadata / thumbnails either way.

### Run

API and worker run in **two separate terminals**:

```powershell
.\scripts\run-api.ps1     # in one terminal
```

```powershell
.\scripts\run-worker.ps1  # in another
```

→ Open `http://localhost:8888` → sign in with **admin / admin**.

### Desktop app (optional)

Instead of a browser plus separate terminals, the **desktop app** combines
the gallery viewer with server management — start/stop/restart the Web/API
+ indexing worker + ML worker, with live logs and indexing progress.
Minimise and it stays in the tray so the workers keep running.

In PowerShell (a desktop-only venv — PySide6):

```powershell
cd desktop
python -m venv .venv
.\.venv\Scripts\python -m pip install -r requirements.txt
.\.venv\Scripts\python app.py
```

→ Hit `▶ 전체 시작` on the **server manager**. Workers launch with the
project's own `.venv` python (auto-detected; change it under "경로 설정" if
needed). For the single-file `MyPhotos.exe` build and full usage, see
[desktop/README.md](../../desktop/README.md).

### ML auto-classify (optional)

**1) Download the models (one time)** — the installer is a Bash script,
so run it from **Git Bash** (`MINGW64`). PowerShell mangles the
backslashes in `.\scripts\…` even when calling out to `bash`.

In Git Bash:

```bash
./scripts/install-ml-models.sh
```

From PowerShell if you really must, use forward slashes inside the
quoted -c string:

```powershell
bash -c "./scripts/install-ml-models.sh"
```

No bash at all? Grab the six ONNX files (~140 MB) from the
[Release page](https://github.com/saintsc-ai/MyPhotos/releases) into
`data\models\` manually.

**2) Run the ML worker** — third PowerShell terminal:

```powershell
.\scripts\run-ml-worker.ps1
```

If the models aren't there yet, the worker boots, logs
"model missing", and sits idle — finish step 1 first then start it.

### Register the photo root

1. Open `http://localhost:8888/admin.html`
2. **사진 폴더 (Roots)** → **새 폴더 추가**
3. Enter the absolute path Windows-style: `D:\Photos` (backslashes)
4. **시험 (Sample, 200)** → watch **색인 (Indexing)** tab

> `taken_at` / GPS EXIF edits and rotation work on Windows, but the
> NTFS permission model differs from POSIX so some failure modes
> manifest differently. Avoid pointing the root at a OneDrive /
> Dropbox-synced folder — sync file locks can clash.

### Code change → restart

API and worker don't auto-reload (uvicorn `--reload` is too disruptive
for the indexing worker). Ctrl+C in both terminals → relaunch.

### Production on Windows: prefer Docker

There's no systemd on Windows, so for background-service operation pick
one of:

1. **Docker Desktop on Windows** — follow the
   [Docker guide](docker.md) as-is. Compose auto-restarts containers
   and shows status in the system tray.
2. **WSL2 + systemd** — follow the [Linux guide](linux.md) inside a
   WSL2 Ubuntu. Looks like Windows but runs Linux underneath.
3. **Windows Task Scheduler** — register `run-api.ps1` / `run-worker.ps1`
   as boot-time tasks. No systemd-level process supervision.

For a family photo catalog in production, a NAS install
(Synology / generic Linux) or Docker is significantly more reliable
than running directly on Windows.

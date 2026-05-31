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
- Python 3.11 가상환경 (`.venv\`)
- `pyproject.toml` 의존성 설치
- `vendor\windows-x64\`에 exiftool/ffmpeg 다운로드 (선택적, 스크립트가 물음)

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

## 실행

API와 워커는 **별도 터미널 두 개**에서 띄웁니다:

```powershell
.\scripts\run-api.ps1     # 한 터미널
```

```powershell
.\scripts\run-worker.ps1  # 다른 터미널
```

→ 브라우저에서 `http://localhost:8888` → **admin / admin** 로그인.

ML 자동 분류까지 쓰려면 세 번째 터미널:

```powershell
.\scripts\run-ml-worker.ps1
```

먼저 모델 한 번 받아두기 (Bash 스크립트지만 Git for Windows의 bash에서 실행
가능, 또는 직접 Release URL에서 6개 ONNX 파일을 `data\models\` 아래에 받으면 됨):

```powershell
bash .\scripts\install-ml-models.sh
```

## 사진 폴더 등록

1. `http://localhost:8888/admin.html` 접속
2. **사진 폴더** 탭 → **새 폴더 추가**
3. 절대 경로 입력 — Windows는 `D:\Photos` 형식 그대로 (백슬래시)
4. **시험** 버튼 → 200장 샘플 → **색인** 탭에서 진행 확인

> Windows에서 `taken_at` / GPS EXIF 편집과 회전 기능은 정상 동작하지만,
> NTFS 권한 모델이 POSIX와 달라 일부 케이스에서 `Permission denied`가 다른
> 형태로 나타날 수 있습니다. 사진 폴더가 OneDrive / Dropbox 동기화 대상이면
> 동기화 파일 잠금과 충돌할 수 있으니 주의.

## 코드 변경 → 재시작

API와 워커는 코드 변경 시 자동 재시작 안 됩니다 (uvicorn `--reload`는
인덱싱 워커에 영향이 큼). 두 터미널에서 각각 Ctrl+C → 다시 실행.

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
- Python 3.11 venv in `.venv\`
- `pyproject.toml` deps
- exiftool / ffmpeg into `vendor\windows-x64\` (optional, script prompts)

### Run

API and worker run in **two separate terminals**:

```powershell
.\scripts\run-api.ps1     # in one terminal
```

```powershell
.\scripts\run-worker.ps1  # in another
```

→ Open `http://localhost:8888` → sign in with **admin / admin**.

For ML auto-classify, a third terminal:

```powershell
.\scripts\run-ml-worker.ps1
```

Download models once first (Bash script, runs in Git for Windows' bash,
or just download the six ONNX files manually from the Release into
`data\models\`):

```powershell
bash .\scripts\install-ml-models.sh
```

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

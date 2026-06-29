# MyPhotos Desktop App

Windows / macOS 데스크톱 앱. PySide6로 만든 하나의 창에 두 가지가 들어
있습니다:

1. **갤러리(뷰어)** — 기존 웹 프런트엔드를 QWebEngine으로 임베드.
   원격 NAS든 이 앱이 직접 돌리는 로컬 서버든 어느 MyPhotos 서버에나
   붙습니다. 로그인 쿠키는 영구 저장돼 두 번째 실행부터 바로 갤러리로
   들어갑니다.
2. **서버 관리** — 서버 3개 프로세스(**Web/API + 인덱싱 워커 + ML
   워커**)를 앱에서 직접 **시작 / 정지 / 재시작**, 라이브 로그 확인,
   **인덱싱 진행 상태**(작업 큐 + 사진 파이프라인) 모니터링. 터미널 없이
   Windows·Mac에서 MyPhotos를 단독 실행·운영할 수 있습니다.

**최소화하거나 창을 닫으면 트레이로 들어가 계속 실행**됩니다(관리 중인
워커가 죽지 않도록). 완전히 끄려면 트레이 아이콘 → **종료**(모든 관리
프로세스를 먼저 정지한 뒤 앱이 닫힙니다).

## 두 가지 사용 모드

| 모드 | 설명 |
| --- | --- |
| **로컬 서버 운영** | 이 PC(소스 체크아웃 + venv)에서 서버를 직접 띄워 쓴다. 첫 실행 시 **서버 관리** 화면으로 시작 → `▶ 전체 시작`. 갤러리는 자동으로 `http://127.0.0.1:포트`를 가리킨다. |
| **원격 뷰어** | NAS 등 다른 서버에 붙어서 보기만 한다. 툴바 **서버 변경**으로 주소 입력. (이 경우 `/admin`은 브라우저에서 — 데스크톱 뷰어에선 차단) |

> 로컬 서버 관리 모드는 **MyPhotos 소스 체크아웃과 프로젝트 venv**가
> 있어야 합니다(워커가 FastAPI/onnxruntime 등 전체 의존성을 필요로 함).
> 앱이 자동 감지하지만, 못 찾으면 **서버 관리 → 경로 설정**에서
> 프로젝트 폴더와 venv의 `python` 경로를 지정하세요.

### 서버 관리 화면

- **로컬 서버 설정** — 감지된 프로젝트 폴더 / Python / 주소(`✓`·`✗`로
  유효성 표시). `경로 설정`으로 수정.
- **ML 가속 (GPU)** — ML 워커가 쓸 ONNX 실행 장치를 드롭다운으로 선택:
  `자동`(기본 — GPU 있으면 자동 사용, 없으면 CPU) · `CPU 전용` ·
  `DirectML`(Windows, 아무 GPU·설치 간단) · `NVIDIA CUDA`(최고 성능) ·
  `OpenVINO`(Intel). 자동 모드면 GPU용 onnxruntime만 깔려 있으면 별도
  설정 없이 GPU를 씁니다. 선택은 ML 워커 실행 시
  환경변수로 주입되며 `config/local.toml`은 건드리지 않습니다 — NAS는 CPU,
  색인용 PC는 GPU로 같은 체크아웃을 다르게 굴릴 수 있습니다. **`GPU 확인`**
  버튼이 프로젝트 venv의 onnxruntime가 그 장치를 실제로 지원하는지
  점검하고, 없으면 설치할 패키지(`onnxruntime-directml` 등)를 알려줍니다.
  바꾸면 ML 워커 재시작 시 적용됩니다(앱이 재시작을 물어봄).
  > 강력한 PC에서 초기 백로그를 빠르게 처리한 뒤 `data/catalog.db` +
  > `data/thumbs` + `data/proxies`를 NAS로 옮기고 `roots.abs_path`만 NAS
  > 경로로 바꾸면 초기 적재가 끝납니다.
- **전체 시작 / 전체 종료 / 전체 재시작** — 3개를 한 번에. (시작은
  API → 워커 → ML 순으로 약간 시차를 둬 부팅 시 DB 경합을 줄임)
- **프로세스** — 각 프로세스별 상태 점등(실행 중/시작 중/정지/비정상
  종료) · PID · 업타임 + 개별 시작/정지/재시작 버튼.
- **인덱싱 진행 상태** — `data/catalog.db`를 읽기 전용으로 2초마다
  폴링: 작업 큐(대기/실행/완료/실패) + 사진(총/EXIF/썸네일/분류) +
  진행률 바. (외부 MariaDB/PostgreSQL을 쓰면 이 패널은 비활성.)
- **로그** — 프로세스별 탭으로 표준출력/에러 실시간 표시(최근 5,000줄).

## 개발 환경에서 실행 (빌드 없이)

**Windows (PowerShell)**

```powershell
cd desktop
python -m venv .venv
.\.venv\Scripts\python -m pip install -r requirements.txt
.\.venv\Scripts\python app.py
```

**macOS / Linux**

```bash
cd desktop
python3 -m venv .venv           # 또는: uv venv --python 3.11 .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python app.py
```

> 이 `.venv`는 **데스크톱 앱 전용**(PySide6만)입니다. 서버 워커는 앱이
> **프로젝트 쪽 `.venv`의 python**으로 따로 띄웁니다 — 자동 감지되며,
> 위 "경로 설정"에서 바꿀 수 있습니다.

## 빌드 (단일 실행파일)

**Windows** — `dist\MyPhotos.exe` (대략 **180–220 MB**, Python+Qt6+
Chromium 포함):

```powershell
cd desktop
.\build.ps1
```

**macOS** — `build.ps1`은 PowerShell 전용입니다. mac에선 같은 venv에서
PyInstaller를 직접 호출하세요(`dist/MyPhotos.app` 생성):

```bash
cd desktop
.venv/bin/python -m PyInstaller --clean myphotos.spec
```

> 단일 실행파일 빌드는 **뷰어 용도**에 적합합니다. 로컬 서버 관리
> 기능까지 쓰려면 그 PC에 MyPhotos 소스 체크아웃 + 프로젝트 venv가
> 있어야 합니다(서버 전체를 exe 하나에 넣지는 않습니다).

## 배포 / 아이콘 / 코드사이닝

`desktop/icon.ico`를 두면 앱·트레이 아이콘으로 쓰입니다(없으면 시스템
기본 아이콘). 적용하려면 `myphotos.spec`의 `# icon="icon.ico"` 주석
해제 후 다시 빌드. Windows 미서명 배포 시 SmartScreen 경고(추가 정보 →
실행)는 EV 코드사이닝으로 사라지지만 비용이 듭니다.

## 알려진 함정

| 증상 | 원인 / 해결 |
| --- | --- |
| `전체 시작` 후 API가 바로 비정상 종료 | 포트(기본 8888)를 이미 다른 프로세스가 점유. 로그 탭 확인 후 그 프로세스를 끄거나 `경로 설정`에서 포트 변경 |
| 프로세스가 `python을 찾을 수 없음`으로 안 뜸 | 프로젝트 venv 경로 오류. **경로 설정**에서 `.../.venv/bin/python`(mac) 또는 `...\.venv\Scripts\python.exe`(win) 지정 |
| 진행 상태가 `DB 없음` | 아직 부트스트랩(마이그레이션) 전이거나 외부 DB 사용 중. `alembic upgrade head` 후 다시 |
| 썸네일/동영상 처리 실패 | exiftool/ffmpeg가 PATH에 없음. 앱이 `/opt/homebrew/bin`·`/usr/local/bin`을 PATH에 넣어주지만, 설치돼 있는지 확인(`brew install exiftool ffmpeg`) |
| 트레이로 안 들어감 | 일부 Linux 데스크톱은 트레이 미지원 — 그땐 닫기가 곧 종료(워커도 정지) |
| 첫 실행 시 갤러리 빈 화면 | 로컬 서버를 아직 안 켰거나 원격 주소 오류. **서버 관리**에서 `전체 시작`, 또는 **서버 변경**으로 주소 재입력 |
| 로그인이 자꾸 풀림 | `%APPDATA%\MyPhotos\qweb-storage\`(mac: `~/Library/Application Support/MyPhotos/`) 권한 문제. 폴더 삭제 후 재실행 |

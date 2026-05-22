# MyPhotos

> 한국어 / [English](#english)

직접 운영하는 사진 카탈로그. 메타데이터 인덱싱과 웹 브라우징을 지원합니다.

- **백엔드**: FastAPI + SQLite (WAL, FTS5, R-Tree)
- **워커**: 스캔, EXIF 추출, 썸네일 생성을 담당하는 별도 프로세스
- **저장소**: 기존 사진 폴더는 읽기 전용으로 인덱싱. 썸네일과 DB는 `data/` 아래에 보관
- **대상 호스트**: Synology DSM (DS3622xs+, x86_64), systemd로 실행

## 디렉토리 구조

```
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

## 부트스트랩 (NAS)

전제 조건: Python 3.11+. [uv](https://docs.astral.sh/uv/) 사용을 권장합니다:

```bash
# uv 1회 설치 (사용자 영역, root 불필요)
curl -LsSf https://astral.sh/uv/install.sh | sh
source ~/.bashrc
uv python install 3.11.9
```

이후:

```bash
git clone <repo> /var/services/homes/scsung/myphotos
cd /var/services/homes/scsung/myphotos
./scripts/bootstrap.sh                # uv 자동 감지
# config/local.toml 편집 (최소한 secret_key)
./scripts/install-systemd.sh          # APP_USER 기본값은 현재 사용자
sudo systemctl enable --now myphotos-api myphotos-worker
```

> 참고: uv가 만든 venv에는 기본적으로 `pip`가 없습니다. 임시 설치는
> `uv pip install ...`을, 스크립트 실행은 `.venv/bin/python -m <module>`을 쓰세요.

## 부트스트랩 (Windows 개발 환경)

```powershell
.\scripts\bootstrap.ps1
Copy-Item config\local.example.toml config\local.toml
.\scripts\run-api.ps1     # 한 터미널에서
.\scripts\run-worker.ps1  # 다른 터미널에서
```

## 다른 호스트로 이전

1. `data/`와 `config/local.toml`을 새 호스트로 복사
2. 이 저장소를 `git clone`하고 `bootstrap.sh` 실행
3. `config/local.toml` 편집 (캐시 경로가 다를 수 있음)
4. 관리 UI에서 각 root의 `abs_path`를 새 사진 폴더 위치로 변경
5. `install-systemd.sh` 실행 후 서비스 시작

DB는 단일 SQLite 파일이며, 외부 서비스는 필요 없습니다.

---

## English

Self-hosted photo catalog with metadata indexing and web browsing.

- **Backend**: FastAPI + SQLite (WAL, FTS5, R-Tree)
- **Worker**: separate process for scanning, EXIF extraction, thumbnail generation
- **Storage**: indexes existing folders read-only; thumbnails and DB live inside `data/`
- **Target host**: Synology DSM (DS3622xs+, x86_64) via systemd

## Layout

```
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

## Bootstrap (NAS)

Prerequisite: Python 3.11+ available. Recommended via [uv](https://docs.astral.sh/uv/):

```bash
# one-time uv install (user-local, no root needed)
curl -LsSf https://astral.sh/uv/install.sh | sh
source ~/.bashrc
uv python install 3.11.9
```

Then:

```bash
git clone <repo> /var/services/homes/scsung/myphotos
cd /var/services/homes/scsung/myphotos
./scripts/bootstrap.sh                # detects uv automatically
# edit config/local.toml (secret_key at minimum)
./scripts/install-systemd.sh          # APP_USER=current user by default
sudo systemctl enable --now myphotos-api myphotos-worker
```

> Note: uv-created venvs do not include `pip` by default. Use `uv pip install ...`
> for ad-hoc installs, or `.venv/bin/python -m <module>` to run scripts.

## Bootstrap (Windows dev)

```powershell
.\scripts\bootstrap.ps1
Copy-Item config\local.example.toml config\local.toml
.\scripts\run-api.ps1     # in one terminal
.\scripts\run-worker.ps1  # in another
```

## Porting to a new host

1. Copy `data/` and `config/local.toml` to the new host
2. `git clone` this repo, run `bootstrap.sh`
3. Edit `config/local.toml` (cache path may differ)
4. In the admin UI, update each root's `abs_path` if photo folders moved
5. `install-systemd.sh` and start services

DB is a single SQLite file. No external services required.

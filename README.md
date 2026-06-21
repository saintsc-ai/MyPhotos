# MyPhotos

![MyPhotos map view](images/map.png)

> 한국어 / [English](#english)

직접 호스팅하는 **사진 라이브러리**. 흩어져 있는 사진을 한곳에 모아 메타데이터를 인덱싱하고, 웹 또는 데스크톱 앱에서 둘러봅니다. 원본은 건드리지 않습니다.

- **백엔드**: FastAPI + SQLite (WAL, FTS5, R-Tree) — 외부 MariaDB / PostgreSQL도 지원
- **워커 2개**: 인덱싱 워커(스캔/EXIF/썸네일) + ML 워커(객체 검출/CLIP 임베딩/얼굴 검출·클러스터링)
- **저장소**: 기존 사진 폴더는 읽기 전용으로 인덱싱. 썸네일과 DB는 `data/` 아래에 보관
- **자동 분류** (선택): YOLOv8(객체) + CLIP(주제/장면) + YuNet/SFace(얼굴) + OCR(RapidOCR 텍스트 추출) — 모두 ONNX, CPU 전용. 신규 사진 자동 처리(설정) 지원
- **검색**: FTS5 통합 검색(파일명·태그·댓글·**OCR 텍스트**) + 날짜/GPS/텍스트 유무 필터
- **다중 사용자**: 로그인 · 사용자·폴더 단위 권한(ACL) · 업로드 · 공개 공유 링크(다운로드 횟수 제한·EXIF GPS 제거 옵션) · 별점/댓글/태그
- **데스크톱 앱** (Windows / macOS): 갤러리 뷰어 + 서버 관리(워커 시작/정지/재시작 · 진행 상태 · 로그, 트레이 상주) — [desktop/](desktop/)
- **실행 환경**: Synology DSM(주 대상) · 일반 Linux · Docker · Windows · macOS — systemd / launchd / Windows 서비스로 24/7 운영 가능

> OCR(사진 속 글자 검색)은 선택 기능입니다 — `uv pip install rapidocr` 후 관리 → 색인 → ML 자동 분류에서 실행. 한국어 모델 자동 다운로드. 자세한 설치/사용은 [설치 후 운영 문서](docs/operations/post-install.md#ocr-텍스트-검색-선택)를 참고하세요.

## 디렉토리 구조

```text
myphotos/
├── app/                # 애플리케이션 코드
│   ├── api/            # FastAPI 앱 (uvicorn 엔트리)
│   ├── admin/          # 관리용 CRUD (roots, jobs, ml)
│   ├── worker/         # 스캐너 + 인덱싱 잡 러너 (서비스 엔트리 — systemd/launchd/Win 서비스)
│   ├── worker_ml/      # ML 잡 러너 — YOLO / CLIP / face (별도 서비스 엔트리)
│   └── web/            # HTMX 템플릿 / 정적 파일
├── config/
│   ├── default.toml    # 기본 설정 (커밋됨)
│   └── local.toml      # 호스트별 오버라이드 (커밋 안 됨)
├── data/               # 런타임 (커밋 안 됨) — DB, 썸네일, 모델, 로그, 휴지통
│   └── models/         # ONNX 모델 (yolo / clip / face) — install-ml-models.sh
├── vendor/             # OS별 바이너리 (exiftool, ffmpeg)
├── alembic/            # DB 마이그레이션
├── scripts/            # 부트스트랩, systemd 설치, ML 모델 다운로드/업로드
├── systemd/            # 유닛 템플릿 (api / worker / ml-worker)
└── desktop/            # 데스크톱 앱 (PySide6) — 갤러리 뷰어 + 서버 관리
```

## 설치

대상 환경별로 별도 가이드:

| 환경 | 가이드 |
| --- | --- |
| **Synology NAS** (DSM 7.x, systemd) | [docs/install/synology.md](docs/install/synology.md) |
| **Docker** (DSM Container Manager / Linux+Docker / Windows+Docker Desktop) | [docs/install/docker.md](docs/install/docker.md) |
| **일반 Linux** (Debian/Ubuntu/Fedora/Arch + systemd) | [docs/install/linux.md](docs/install/linux.md) |
| **Windows** (개발용) | [docs/install/windows.md](docs/install/windows.md) |
| **macOS** (개발용 · Intel/Apple Silicon) | [docs/install/macos.md](docs/install/macos.md) |

### 도커 빠른 시작 (마법사)

Docker Desktop / Docker가 설치돼 있다면 `.env`나 compose 파일을 손대지
않고 한 줄로 시작할 수 있습니다 — 대화식 마법사가 사진 폴더 위치 (로컬
폴더 또는 NAS SMB 공유), 포트, 타임존을 물어보고, 컨테이너를 띄운 뒤
브라우저까지 자동으로 엽니다. 그 후 첫 페이지가 웹 마법사로 이어져
관리자 비밀번호 → 사진 폴더 등록 → ML 모델 다운로드까지 끝냅니다.

```bash
# Linux · macOS · Synology SSH
git clone https://github.com/saintsc-ai/MyPhotos.git ~/myphotos
cd ~/myphotos
./scripts/setup.sh
```

```powershell
# Windows (PowerShell)
git clone https://github.com/saintsc-ai/MyPhotos.git $HOME\myphotos
cd $HOME\myphotos
.\scripts\setup.ps1
```

> 단계별로 무엇이 만들어지는지, 수동으로 커스터마이즈하려면 어떻게 하는지는
> [docs/install/docker.md](docs/install/docker.md)의 "1-A · 1-B" 섹션 참고.

설치가 끝난 뒤의 운영은 주제별로 분리되어 있습니다 — 어느 환경(Synology / Linux / Windows)이든 동일하게 적용됩니다.

## 설치 후 운영

| 주제 | 가이드 |
| --- | --- |
| **일상 운영** — 코드 업데이트 / watcher / **스마트폰 백업(PhotoSync)** / 백업 / 트러블슈팅 | [docs/operations/post-install.md](docs/operations/post-install.md) |
| **외부 DB (MariaDB / PostgreSQL)** — DSN 설정, 마이그레이션, 백업 | [docs/operations/external-db.md](docs/operations/external-db.md) |
| **다른 호스트로 이전** — NAS / Linux / Windows 간 (재인덱싱 없이) | [docs/operations/porting.md](docs/operations/porting.md) |
| **HTTPS (선택 · 권장)** — 외부 접속 / PWA 오프라인 / "현재 위치" 기능 | [docs/operations/post-install.md](docs/operations/post-install.md#https-설정-선택--권장) |

각 가이드는 Linux/Synology (systemd)와 Windows (`myphotos.ps1`) 명령을 함께 다룹니다.

## 워커 & 작업 파이프라인

4개 systemd 서비스로 분리. 큐는 **사진 단위 큐(`photo_work`)** 와 **루트 단위 잡 큐(`jobs`)** 두 개를 같이 씁니다.

| 서비스 | 담당 | 큐 |
| --- | --- | --- |
| `myphotos-api` | 웹/API (잡 안 가져감) | — |
| `myphotos-watcher` | inotify 감시 → 디바운스 후 폴더 스캔 트리거 | jobs (생산만) |
| `myphotos-worker` | 폴더 스캔(`discover_root`) · 사진 stage 워커 6스레드 · 정기 잡(중복정리, FTS, 정리 sweeper) · 매트릭스 ⋯ 재작업 (`bulk_retry_stage`) | 둘 다 |
| `myphotos-ml-worker` | `classify_ml` 픽업 → YOLO 객체 · CLIP 분위기 · 얼굴 · OCR (한 잡에서 4개 stage 순차) · `recluster_faces` 수동 트리거 | jobs |

### 사진 1장이 들어오면 (새 업로드 기준)

```text
1. /volume1/photo/... 에 파일 떨어짐
        ├─ watcher (inotify) ──┐
        └─ apscheduler 10분 ───┴─→ jobs.discover_root enqueue

2. worker가 discover_root 픽업 → os.scandir 재귀 walk
   - 새 파일이면 Photo 행 INSERT + photo_work 행 INSERT
     stages = {"index": "pending"}
     priority = 80 + recency boost (오늘 사진 = 84)

3. photo_work 워커 6스레드 중 하나가 claim
   STAGE_ORDER 순서로 stages 순회 (index → transcode → classify → estimate_location)

   a. index → app.worker.index_file.run()
      • SHA-256 스트리밍
      • EXIF (Pillow → exiftool fallback for HEIC/RAW)
      • 썸네일 (Pillow / pillow-heif / exiftool RAW preview / ffmpeg 영상 1프레임)
      • GPS 추출 → PhotoLocation INSERT (source='exif')
      • 라이브 포토 짝 매칭 (.HEIC + .MOV)
      • _maybe_auto_enqueue → stages.classify='pending' (priority=5)
      • _maybe_auto_enqueue_location → stages.estimate_location='pending' (priority=0)

   b. transcode (영상만)
      • mp4/mov 등 브라우저 직접 재생 가능 → skip
      • .avi/.mkv/.3gp → ffmpeg H.264 proxy → proxy_status='done'

   c. classify → ml-worker 위임
      • photo_work는 jobs.classify_ml 1건만 enqueue
      • ml-worker가 픽업 → 객체/CLIP/얼굴/OCR 4단계 순차 처리 → 각 stage status 갱신

   d. estimate_location (taken_at 있고 실제 GPS 없는 사진만)
      • 같은 폴더/상위 폴더의 시간상 가까운 GPS 있는 사진을 anchor로 보간
      • PhotoLocation INSERT (source='estimated')

4. 모든 stage 끝나면 photo_work 행 자동 DELETE
```

### 우선순위 밴드 (`photo_work.priority`)

`claim_one()`이 `ORDER BY priority DESC, photo_id ASC`로 다음 행 픽업.

| 우선순위 | 무엇 |
| --- | --- |
| 100 | 매트릭스 ⋯ → 실패만 재작업 |
| 80 + recency 0~4 | **새 파일 발견** (discover가 신규/변경 enqueue) |
| 50 | 매트릭스 ⋯ → 미처리만 작업 |
| 10 | 매트릭스 ⋯ → 전체 재작업 (배경 sweep) |
| 5 | auto-enqueue downstream (classify, lazy transcode) |
| 0 | auto-enqueue geo_estimate |

새 사진(80+)이 항상 배경 sweep(10) 보다 앞 → 업로드한 사진의 썸네일을 GPS 추정 200k 뒤에서 기다리는 일 없음.

### 신뢰성

- **`claim_token` (UUID)** + atomic UPDATE-with-subquery로 두 워커가 같은 행 가져가는 경합 차단
- **photo_work sweeper** (5분 주기): `claimed_at`가 `worker.job_lease_seconds`(기본 600초)보다 오래된 행은 자동 풀림 — 워커 크래시/SIGKILL 후 영구 잠금 방지
- **stage 단위 commit**: 한 stage 실패해도 다음 stage 계속 (`stages.X='failed'` 기록, `last_error` 저장)
- **stages JSON merge**: 같은 사진에 중복 enqueue 시 새 stage만 추가, 이미 ok인 건 그대로
- **cooperative shutdown**: SIGTERM 시 stage 사이 `_stop` 체크 → 현재 stage 끝나면 즉시 release하고 종료

### 진행 상황 보기

가장 직관적인 건 관리 → 색인 진행 탭의 **단계별 진행 매트릭스** (스테이지별 대기/진행중/완료/실패 + ⋯ 메뉴로 재작업). SQL로 직접 보려면:

```bash
# photo_work (사진 단위 큐) 현황
sqlite3 data/catalog.db "SELECT COUNT(*) AS rows, COUNT(claim_token) AS claimed FROM photo_work"

# 사진별 stage status 분포
sqlite3 data/catalog.db "SELECT 'exif', exif_status, COUNT(*) FROM photos WHERE status='active' GROUP BY exif_status"

# jobs (루트/관리자 잡 큐) 현황
sqlite3 data/catalog.db "SELECT kind, status, COUNT(*) FROM jobs GROUP BY kind, status ORDER BY kind, status"
```

## 데스크톱 앱 (선택)

브라우저 대신 쓰는 Windows / macOS 데스크톱 앱입니다. 하나의 창에서:

- **갤러리 뷰어** — 웹 프런트엔드를 그대로 임베드(QWebEngine). 원격 NAS든 로컬 서버든 어느 MyPhotos 서버에나 접속. 로그인 세션 유지.
- **서버 관리** — Web/API · 인덱싱 워커 · ML 워커를 앱에서 **시작 / 정지 / 재시작**, 라이브 로그, **인덱싱 진행 상태**(작업 큐 + 사진 파이프라인) 모니터링. 터미널 없이 단독 운영 가능.
- **트레이 상주** — 최소화·닫기 시 트레이로 들어가 워커는 계속 실행. 트레이 메뉴에서 완전 종료.

소스에서 바로 실행(빌드 없이):

```bash
cd desktop
python3 -m venv .venv            # 또는: uv venv --python 3.11 .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python app.py          # Windows: .\.venv\Scripts\python app.py
```

설정·빌드(단일 실행파일)·문제 해결은 [desktop/README.md](desktop/README.md)를 참고하세요.

---

## English

Self-hosted **photo library**. Pulls your scattered photos into one place, indexes their metadata, and lets you browse from the web or a desktop app — your originals are never modified.

- **Backend**: FastAPI + SQLite (WAL, FTS5, R-Tree) — external MariaDB / PostgreSQL also supported
- **Two workers**: indexing (scanning / EXIF / thumbnails) and ML (object detection / CLIP embeddings / face detection + clustering)
- **Storage**: indexes existing folders read-only; thumbnails and DB live inside `data/`
- **Auto-classification** (optional): YOLOv8 (objects) + CLIP (topics/scenes) + YuNet/SFace (faces) + OCR (RapidOCR text extraction) — all ONNX, CPU only; can auto-run on new photos
- **Search**: unified FTS5 (filename / tags / comments / **OCR text**) + date / GPS / has-text filters
- **Multi-user**: login · per-user/folder permissions (ACL) · uploads · public share links (download-count caps, optional EXIF GPS stripping) · ratings/comments/tags
- **Desktop app** (Windows / macOS): gallery viewer + server manager (start/stop/restart workers · progress · logs, tray-resident) — [desktop/](desktop/)
- **Runs on**: Synology DSM (primary) · generic Linux · Docker · Windows · macOS — 24/7 via systemd / launchd / a Windows service

> OCR (search by text in photos) is optional — `uv pip install rapidocr`, then run it from Admin → Indexing → ML auto-classify (Korean model auto-downloads). See [post-install docs](docs/operations/post-install.md) for setup/usage.

## Layout

```text
myphotos/
├── app/                # application code
│   ├── api/            # FastAPI app (uvicorn entry)
│   ├── admin/          # admin CRUD (roots, jobs, ml)
│   ├── worker/         # scanner + indexing job runner (service entry — systemd/launchd/Win service)
│   ├── worker_ml/      # ML job runner — YOLO / CLIP / face (separate service entry)
│   └── web/            # HTMX templates / static
├── config/
│   ├── default.toml    # built-in defaults (tracked)
│   └── local.toml      # per-host overrides (NOT tracked)
├── data/               # runtime (NOT tracked) — DB, thumbs, models, logs, trash
│   └── models/         # ONNX weights (yolo / clip / face) — install-ml-models.sh
├── vendor/             # OS-specific binaries (exiftool, ffmpeg)
├── alembic/            # DB migrations
├── scripts/            # bootstrap, systemd install, ML model download/upload
├── systemd/            # unit templates (api / worker / ml-worker)
└── desktop/            # desktop app (PySide6) — gallery viewer + server manager
```

## Install

Pick the guide that matches your environment:

| Environment | Guide |
| --- | --- |
| **Synology NAS** (DSM 7.x, systemd) | [docs/install/synology.md](docs/install/synology.md) |
| **Docker** (DSM Container Manager / Linux+Docker / Windows+Docker Desktop) | [docs/install/docker.md](docs/install/docker.md) |
| **Generic Linux** (Debian/Ubuntu/Fedora/Arch + systemd) | [docs/install/linux.md](docs/install/linux.md) |
| **Windows** (dev) | [docs/install/windows.md](docs/install/windows.md) |
| **macOS** (dev · Intel/Apple Silicon) | [docs/install/macos.md](docs/install/macos.md) |

### Docker quick start (wizard)

If you've already got Docker Desktop (or Docker on Linux/Synology), one
command kicks off an interactive installer — no hand-editing `.env`
or the compose file. The wizard asks where your photos live (local
folder or NAS SMB share), the host port, and timezone; brings up the
stack; then opens your browser. The first page hands off to a web
wizard that walks you through admin password → photo root → ML model
download.

```bash
# Linux · macOS · Synology SSH
git clone https://github.com/saintsc-ai/MyPhotos.git ~/myphotos
cd ~/myphotos
./scripts/setup.sh
```

```powershell
# Windows (PowerShell)
git clone https://github.com/saintsc-ai/MyPhotos.git $HOME\myphotos
cd $HOME\myphotos
.\scripts\setup.ps1
```

> See [docs/install/docker.md](docs/install/docker.md) sections "1-A / 1-B"
> for what the wizard creates and how to opt out for hand customisation.

Post-install ops are split by topic — they apply equally to every environment (Synology / Linux / Windows).

## Post-install

| Topic | Guide |
| --- | --- |
| **Day-to-day ops** — code update / watcher / **phone backup (PhotoSync)** / backups / troubleshooting | [docs/operations/post-install.md](docs/operations/post-install.md) |
| **External DB (MariaDB / PostgreSQL)** — DSN setup, migration, backups | [docs/operations/external-db.md](docs/operations/external-db.md) |
| **Porting to a new host** — across NAS / Linux / Windows (no re-index) | [docs/operations/porting.md](docs/operations/porting.md) |
| **HTTPS (optional · recommended)** — internet access / PWA offline / "use my location" | [docs/operations/post-install.md](docs/operations/post-install.md#https-설정-선택--권장) |

Each guide covers both Linux/Synology (systemd) and Windows (`myphotos.ps1`) commands.

## Workers & job pipeline

Four systemd services. Two queues run side-by-side: a **per-photo queue** (`photo_work`) and a **root/admin job queue** (`jobs`).

| Service | Role | Queues |
| --- | --- | --- |
| `myphotos-api` | Web / API (doesn't claim jobs) | — |
| `myphotos-watcher` | inotify → debounce → trigger folder scan | jobs (producer only) |
| `myphotos-worker` | `discover_root` · per-photo stage workers (6 threads) · periodic jobs (dedup, FTS, sweeper) · admin matrix retries (`bulk_retry_stage`) | both |
| `myphotos-ml-worker` | picks up `classify_ml` → YOLO objects · CLIP · faces · OCR (all four substages in one job) · admin-triggered `recluster_faces` | jobs |

### What happens when a new photo arrives

```text
1. File lands in /volume1/photo/...
        ├─ watcher (inotify) ──┐
        └─ apscheduler 10-min ─┴─→ jobs.discover_root enqueued

2. worker picks up discover_root → recursive os.scandir walk
   - New file → INSERT Photo row + INSERT photo_work row
     stages = {"index": "pending"}
     priority = 80 + recency boost (today's photo = 84)

3. One of 6 photo_work threads claims the row
   Walks STAGE_ORDER (index → transcode → classify → estimate_location)

   a. index → app.worker.index_file.run()
      • SHA-256 (streaming)
      • EXIF (Pillow → exiftool fallback for HEIC/RAW)
      • Thumbnail (Pillow / pillow-heif / exiftool RAW preview / ffmpeg single frame)
      • GPS extract → INSERT PhotoLocation (source='exif')
      • Live Photo pairing (.HEIC + .MOV)
      • _maybe_auto_enqueue → stages.classify='pending' (priority=5)
      • _maybe_auto_enqueue_location → stages.estimate_location='pending' (priority=0)

   b. transcode (videos only)
      • mp4/mov etc. browser-playable → skip
      • .avi/.mkv/.3gp → ffmpeg H.264 proxy → proxy_status='done'

   c. classify → delegated to ml-worker
      • photo_work just enqueues one jobs.classify_ml row
      • ml-worker picks it up → runs objects/CLIP/faces/OCR in sequence

   d. estimate_location (only photos with taken_at and no real GPS)
      • Interpolates from time-nearest GPS-carrying photo in the same folder
      • INSERT PhotoLocation (source='estimated')

4. All stages settle → photo_work row auto-DELETEd
```

### Priority bands (`photo_work.priority`)

`claim_one()` orders rows by `priority DESC, photo_id ASC`.

| Priority | Source |
| --- | --- |
| 100 | matrix ⋯ → retry failed |
| 80 + recency 0..4 | **newly-discovered file** (discover enqueues new/changed) |
| 50 | matrix ⋯ → run pending |
| 10 | matrix ⋯ → retry all (background sweep) |
| 5 | downstream auto-enqueue (classify, lazy transcode) |
| 0 | auto-enqueued geo_estimate |

New photos (≥80) always clear ahead of background sweeps (10) — uploads never wait behind a 200k-row geo-estimate.

### Reliability

- **`claim_token` (UUID)** + atomic UPDATE-with-subquery — no two workers ever take the same row.
- **photo_work sweeper** runs every 5 min: rows whose `claimed_at` exceeds `worker.job_lease_seconds` (default 600s) get released — recovers from worker crashes / SIGKILL.
- **Per-stage commit**: a failure in one stage is recorded (`stages.X='failed'`, `last_error` saved) and the walk continues with the remaining stages.
- **stages JSON merge**: re-enqueueing a stage on a photo merges in; stages already `ok` are left alone.
- **Cooperative shutdown**: SIGTERM checks `_stop` between stages — current stage finishes, claim is released, worker exits cleanly.

### Reading progress

The clearest view is **Admin → Indexing → Per-stage progress matrix** (counts + ⋯ menu to retry). For raw SQL:

```bash
# photo_work (per-photo queue)
sqlite3 data/catalog.db "SELECT COUNT(*) AS rows, COUNT(claim_token) AS claimed FROM photo_work"

# Per-stage status spread on photos
sqlite3 data/catalog.db "SELECT 'exif', exif_status, COUNT(*) FROM photos WHERE status='active' GROUP BY exif_status"

# jobs (root/admin queue)
sqlite3 data/catalog.db "SELECT kind, status, COUNT(*) FROM jobs GROUP BY kind, status ORDER BY kind, status"
```

## Desktop app (optional)

A Windows / macOS desktop app to use instead of a browser. One window, two things:

- **Gallery viewer** — embeds the web frontend (QWebEngine); connects to any MyPhotos server (remote NAS or a local one). Login session persists.
- **Server manager** — **start / stop / restart** the Web/API + indexing worker + ML worker from the app, with live logs and **indexing progress** (job queue + photo pipeline). Run MyPhotos standalone, no terminal needed.
- **Tray-resident** — minimising or closing keeps it running in the tray so the workers stay up; quit fully from the tray menu.

Run from source (no build):

```bash
cd desktop
python3 -m venv .venv            # or: uv venv --python 3.11 .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python app.py          # Windows: .\.venv\Scripts\python app.py
```

See [desktop/README.md](desktop/README.md) for configuration, single-file builds, and troubleshooting.

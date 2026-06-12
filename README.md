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

작업은 **하나의 큐(`jobs` 테이블)** 에 종류(`kind`)별로 섞여 들어가고, 워커마다 **자기 종류만** 집어갑니다(`claim_one(kinds=…)` — 서로 안 훔침). 처리 순서는 `우선순위 높은 것 → 오래된 것`(`ORDER BY priority DESC, id ASC`).

| 워커(서비스) | 담당 kind | 하는 일 |
| --- | --- | --- |
| **색인 워커** `myphotos-worker` | `discover_root` · `index_file` · `dedup_cleanup` · `transcode_proxy` · `reindex_fts` | 폴더 스캔 → 해시·EXIF·**썸네일**, 중복정리, 동영상 변환, 검색 재색인 |
| **ML 워커** `myphotos-ml-worker` | `classify_objects` · `classify_embedding` · `detect_faces` · `ocr_text` · `recluster_faces` | YOLO 객체 · CLIP 분위기 · **얼굴** · OCR · 인물 재군집 |
| `myphotos-watcher` / `myphotos-api` | (잡 안 가져감) | 실시간 변경 감지 / 웹·API |

진행 순서(사진 1장 기준):

```text
discover_root (스캔)
   └─ index_file (해시 → EXIF → 썸네일)            ← 색인 워커
        └─ (썸네일 준비 + ml.auto_enqueue=on 이면 아래 4개를 큐잉)
           ├─ classify_objects   (YOLO)   ┐  ← ML 워커
           ├─ classify_embedding (CLIP)   │     서로 순서 없음·독립
           ├─ detect_faces       (얼굴)   │     (worker.ml_concurrency 만큼 동시)
           └─ ocr_text           (OCR)    ┘
                 └─ recluster_faces (관리자 수동: 인물 묶기 재정렬)
```

- **ML 4단계는 `index_file`(썸네일)이 끝나야** 큐에 들어갑니다(썸네일 위에서 추론).
- **4단계끼리는 의존성이 없습니다** — 같은 사진이라도 완료 순서가 다를 수 있음.
- 객체·CLIP·얼굴 잡은 따로지만 사진의 `classify_status` 한 값을 공유합니다(OCR은 `ocr_status` 별도).
- `auto_enqueue`는 **색인 워커**가 읽으므로, 켠 뒤엔 색인 워커도 재시작해야 신규 사진에 적용됩니다.

진행 현황은 종류별로 묶어서 봐야 의미가 있습니다:

```bash
sqlite3 data/catalog.db "select kind, status, count(*) c from jobs group by kind, status order by kind, status;"
# status: queued(대기) · running(처리 중) · done(완료) · failed(실패)
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

All work goes through **one queue** (the `jobs` table), with each row tagged by `kind`. Each worker claims **only its own kinds** (`claim_one(kinds=…)` — they never steal each other's), processed `priority DESC, id ASC`.

| Worker (service) | Kinds | Does |
| --- | --- | --- |
| **Indexing** `myphotos-worker` | `discover_root` · `index_file` · `dedup_cleanup` · `transcode_proxy` · `reindex_fts` | scan → hash · EXIF · **thumbnails**, dedup, video transcode, search reindex |
| **ML** `myphotos-ml-worker` | `classify_objects` · `classify_embedding` · `detect_faces` · `ocr_text` · `recluster_faces` | YOLO objects · CLIP topics · **faces** · OCR · face re-clustering |

Per-photo order:

```text
discover_root → index_file (hash → EXIF → thumbnail)        [indexing worker]
   └─ (thumbnail ready + ml.auto_enqueue=on → enqueue the 4 ML stages)
      classify_objects · classify_embedding · detect_faces · ocr_text   [ML worker]
        - depend on index_file (run on the thumbnail); no order among themselves
        - run worker.ml_concurrency at a time
      └─ recluster_faces (admin-triggered: regroup people)
```

`auto_enqueue` is read by the **indexing worker**, so restart it after toggling. Read progress per kind:

```bash
sqlite3 data/catalog.db "select kind, status, count(*) c from jobs group by kind, status order by kind, status;"
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


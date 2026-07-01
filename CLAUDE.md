# MyPhotos 개발 가이드

## 프로젝트 개요

자가 호스팅 **사진 라이브러리**. 흩어진 사진을 한 곳에서 색인·탐색하고, 웹/데스크톱 앱에서 둘러본다. **원본 파일은 절대 수정하지 않는다**(회전 등은 별도 규칙).

- **백엔드**: FastAPI + SQLite(WAL · FTS5 · R-Tree). 외부 MariaDB / PostgreSQL도 지원.
- **워커 2종(프로세스 분리)**: 인덱싱(스캔·EXIF·썸네일) / ML(YOLO 객체·CLIP 임베딩·얼굴 검출+군집·OCR).
- **저장**: 기존 폴더를 읽기 전용으로 색인. 썸네일·DB는 `data/` 안에.
- **멀티유저**: 로그인 · 사용자/폴더 ACL · 업로드 · 공개 공유 링크 · 별점/댓글/태그.
- **배포처**: Synology DSM(주력) · 일반 Linux · Docker · Windows · macOS. 데스크톱 앱(PySide6)으로 단독 운영도 가능([desktop/](desktop/)).

## 프로젝트 구조

```
app/
├── api/            # FastAPI 앱 (uvicorn 엔트리: app.api.main:app)
│   ├── main.py         # 앱 조립 + include_router (prefix=/api)
│   ├── routes_photos.py# 사진/검색/지도/위치 등 핵심 API
│   ├── routes_setup.py # 최초 셋업 마법사
│   └── deps.py         # 인증/권한 의존성
├── admin/          # 관리자 전용 라우터 (roots/jobs/indexing/ml/settings/trash/…)
├── worker/         # 인덱싱 워커 (dispatcher, index_file, exif, thumbs, jobs, photo_work)
├── worker_ml/      # ML 워커 (dispatcher, clip, yolo, faces, ocr, _ort, jobs)
├── scanner/        # 루트 스캔/디스커버리 (discover, utils)
├── watcher/        # 파일시스템 감시 (watchdog → discover_root)
├── tools/          # 일회성 운영 CLI (python -m app.tools.<name>)
├── web/static/     # 프론트엔드 (index.html, admin.html, css/, js/, sw.js, i18n/)
├── models.py       # SQLAlchemy ORM (전체 테이블)
├── config.py       # TOML 설정(pydantic) — default.toml + local.toml
├── db.py           # 엔진/세션 + SQLite PRAGMA
└── paths.py        # DATA_DIR/THUMBS_DIR/… (MYPHOTOS_DATA로 재배치)
alembic/versions/   # 마이그레이션 (현재 최신 0037)
systemd/            # *.service.in (myphotos-api/worker/ml-worker/watcher)
docs/               # 설치/운영 문서
```

## 코딩 컨벤션

### 데이터베이스
- ORM은 **SQLAlchemy**(`app/models.py`). 스키마 변경은 **alembic 마이그레이션**으로만(`alembic/versions/`).
- 기본 DB는 **SQLite + WAL**. 동시 쓰기 경합은 `PRAGMA busy_timeout=60000`으로 흡수([db.py:88-96](app/db.py#L88)). **외부 MariaDB/PostgreSQL** 병행 지원 → SQLite 전용 문법(예: `VACUUM INTO`, R-Tree) 사용 시 분기 필요.
- "database is locked"(SQLITE_BUSY)는 **일시적**일 수 있음 → 워커 디스패처는 `is_transient_lock()` 판정 후 `fail(requeue=True)`로 재큐잉(영구 실패로 처리 금지).
- 다중 라이터가 도는 상태에서 **DDL(ALTER/CREATE) 실행 금지** → 마이그레이션은 워커 정지 후(중요 규칙 참고).

### FastAPI 라우터 패턴
- 라우터는 `APIRouter`로 만들고 `app/api/main.py`에서 **`include_router(..., prefix="/api", dependencies=...)`**로 등록([main.py:382+](app/api/main.py#L382)).
- 권한대는 의존성으로 건다: **`admin_only`**(관리자 전용) / **`auth_only`**(로그인 필요) / 공개(prefix만). 새 관리자 API는 `app/admin/`에 두고 `admin_only`로 묶는다.

### 권한 제어
- 사진 단위 접근은 **`require_photo_level(db, user, photo, "read"|"edit"|...)`**(`app/auth_acl.py`)로 검사. 루트/폴더 ACL은 `root_acl`/`folder_acl`.
- 관리자 판정은 `User.is_admin`. 라우터 레벨(`admin_only`)과 핸들러 레벨(`require_*`) 둘 다 활용.

### API 응답 형식
- 성공은 Pydantic 응답 모델 또는 `dict`, 오류는 **`HTTPException(status_code, detail)`**. `detail`은 프론트가 그대로 노출할 수 있으니 사용자용 문구로.

### 프론트엔드 (vanilla JS, 빌드 없음)
- **번들러 없음.** 메인 앱은 [index.html](app/web/static/index.html)의 **인라인 `<script>`**(대형)에 있고, 공용/패널은 `js/`의 **IIFE 모듈**이 `window.*` 전역으로 노출(`common.js`, `api.js`, `i18n.js`, `js/panels/*.js`).
- HTML/JS는 `<script src>` 순서 로딩. 새 전역은 사용처보다 **먼저** 로드되게 순서 확인.
- 파일 참조는 클릭 가능한 경로로. **오프라인 아님** — Leaflet 등 일부 외부 CDN 사용(예: index.html의 unpkg leaflet). 새 외부 의존은 신중히.

## 워커 & 작업 큐

### 인덱싱 워커 vs ML 워커 (별도 프로세스)
- **인덱싱 워커**(`python -m app.worker.main`, systemd `myphotos-worker`): 스캔·EXIF·썸네일·GPS + `photo_work` 단계별 풀.
- **ML 워커**(`python -m app.worker_ml.main`, `myphotos-ml-worker`): YOLO/CLIP/얼굴/OCR. `ml_concurrency` 기본 2.

### 작업 큐 — `photo_work`(단계) + `jobs`(레거시)
- 신모델 **`photo_work`**: 사진당 1행 + `stages` JSON(`{"index":"pending","classify":"ok",...}`). 단계는 **`STAGE_ORDER = (index, transcode, classify, estimate_location)`** 순서로 진행([worker/photo_work.py](app/worker/photo_work.py)).
- 단계 요청은 **`enqueue_stage(db, photo_id, stage, *, priority=…)`**(INSERT-or-UPDATE, 커밋은 호출자). 우선순위는 `PRIO_*` 밴드(`PRIO_NEW_INDEX=80` > `PRIO_USER_RUN_PENDING=50` > `PRIO_AUTO_DOWNSTREAM=5` > `PRIO_AUTO_GEO=0`).
- 단계별 스레드 수는 `[worker].photo_work_threads`(기본 index=2/transcode=1/classify=2/estimate_location=1) — 느린 단계가 빠른 단계를 굶기지 않게 분리.

### ML 실행 프로바이더 (CPU/GPU 자동)
- 모든 ONNX 세션은 **`app/worker_ml/_ort.make_session()`** 하나로 생성. `[ml].onnx_providers`(기본 `["auto"]`) 또는 환경변수 **`MYPHOTOS_ONNX_PROVIDERS`**로 결정.
- **`"auto"`**는 설치된 onnxruntime가 실제 노출하는 provider 중 최선을 고름(CUDA>DirectML>ROCm>OpenVINO>CoreML), 없으면 CPU. 일반 onnxruntime는 CPU만 → NAS는 자동 CPU. 데스크톱 앱이 GPU 감지 시 런타임 자동 교체.

## 주요 테이블 (`app/models.py`)

- **`photos`**: 사진 1행. `sha256`(내용 키), `rel_path`(루트 상대·POSIX·NFC), 상태 컬럼(`exif_status`/`thumb_status`/`objects_status`/`clip_status`/`faces_status`/`ocr_status`/`classify_status`/`proxy_status`).
- **`photo_work`**: 단계 작업 큐(위 참고).
- **`photo_locations`**: 사진당 1행(`photo_id` PK). `latitude/longitude/altitude` **공용 컬럼** + **`source`**(`exif`/`estimated`/`user`/NULL=legacy exif)로 종류 구분. 추정이 실측을 덮지 않도록 `source != 'estimated'`면 보호.
- **`roots`**: 색인 루트. `abs_path`만 호스트별로 다르고 `rel_path`는 OS 무관 → 이전 시 `abs_path`만 재작성(`python -m app.tools.cutover`).
- 그 외: `jobs`(레거시 큐), `photo_faces`/`face_clusters`/`photo_objects`/`photo_embeddings`(ML 산출), `photo_tags`/`photo_auto_tags`, `users`/`root_acl`/`folder_acl`, `shares`/`share_items`, `audit_log`.

## 자주 쓰는 패턴

### 확인/알림 다이얼로그 ([common.js](app/web/static/js/common.js))
- `window.uiAlert(msg)` / `window.uiConfirm(msg)` / `window.uiPrompt(msg, default)` — 화면 중앙 커스텀 다이얼로그. `uiConfirm/uiPrompt`는 **async**라 `await`. `window.alert`도 `uiAlert`로 오버라이드됨.

### i18n ([i18n.js](app/web/static/js/i18n.js), 카탈로그 `web/static/i18n/`)
- 번역은 **`window._t(key, fallback)`** / `_tn(key, fallback, params)`([common.js:59](app/web/static/js/common.js#L59)). 카탈로그에 키가 없으면 **fallback 사용**.
- 갤러리(index.html) 본문은 대부분 미번역이라 `_t(key, "한국어 기본")` 형태로 fallback만 쓰는 곳이 많음(예: 날짜 피커 `dp.*`). data-i18n 속성은 로그인/관리 등 번역된 화면에서 사용.
- 문자열은 **NBSP를 `&nbsp;` 엔티티로 넣지 말 것** — `textContent` 렌더에서 깨짐. 실제 비분리 공백 문자 사용(과거 수정 사례).

### 커스텀 날짜 피커 (index.html `attachCustomDatePicker`)
- 모든 `<input type="date">`를 커스텀 피커로 감싼다(네이티브 달력이 브라우저 UI 언어를 따라가는 문제 회피). 헤더에 **연/월 드롭다운**(즉시 점프) + **직접 타이핑**(YYYY-MM-DD) 지원. 값은 `input.value`에 ISO로 유지해 기존 읽기 코드와 호환.
- 외부에서 `input.value`를 바꾼 뒤에는 **`change` 이벤트를 디스패치**해야 트리거 텍스트가 갱신됨.

### 서비스워커 캐시 ([sw.js](app/web/static/sw.js))
- 셸 자산은 `SHELL_CACHE = myphotos-shell-${VERSION}`로 캐시. **정적 셸을 바꾸면 `VERSION`을 올려야**(현재 `v2`) 클라이언트가 옛 캐시를 버림.

### 테마
- **다크가 기본**, `body.light`가 라이트. 새 CSS는 두 테마 모두 규칙 추가(`.x { … } body.light .x { … }`).

## 실행 방법

```bash
# 개발: API
.venv/bin/python -m uvicorn app.api.main:app --host 127.0.0.1 --port 8888 --reload
# 워커
.venv/bin/python -m app.worker.main         # 인덱싱
.venv/bin/python -m app.worker_ml.main      # ML
.venv/bin/python -m app.watcher.main        # 파일 감시(선택)
# 마이그레이션
.venv/bin/alembic upgrade head
```
데스크톱 앱(서버 관리 포함)은 [desktop/README.md](desktop/README.md).

## 환경 변수
- **`MYPHOTOS_DATA`**: `data/` 재배치(catalog.db/thumbs/proxies/…).
- **`MYPHOTOS_ONNX_PROVIDERS`**: ML 워커 실행 프로바이더 오버라이드(`auto` / `DmlExecutionProvider,CPUExecutionProvider` 등).
- 설정 본체는 `config/default.toml` + `config/local.toml`(관리 UI가 local.toml을 갱신).

## 중요 규칙

### DB 마이그레이션 (워커 정지 후)
- SQLite에서 **워커가 도는 채로 `alembic upgrade`(ALTER/CREATE) 하면 "database is locked"로 실패**. 순서: 서비스 정지(`myphotos-ml-worker myphotos-worker myphotos-watcher myphotos-api`) → python 프로세스가 DB를 안 잡는지 확인 → `PRAGMA wal_checkpoint(TRUNCATE)` → `alembic upgrade head` → 재시작.

### 배포 (Synology 주력)
- 운영 서버는 **`git pull` 후 `sudo systemctl restart …`**로 반영(pull 기반). 정적 파일만 바꿨으면 재시작 없이 새로고침으로도 되지만, 서비스워커 캐시 때문에 **`VERSION` 올림 + 강력 새로고침**이 필요할 수 있음.

### 커밋 규칙
- 요청 시에만 커밋/푸시. 기본 브랜치(`main`)면 그대로 push하는 pull-기반 워크플로. 커밋 메시지 끝에:
  `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`

### 정적 파일 변경 후 검증
- 인라인 JS(index.html) 수정 후 **구문/균형 확인**(가능하면 헤들리스 Chrome 렌더). 실제로 이 저장소는 CSS/그리드/피커 변경을 **`--headless=new --screenshot` 하니스**로 시각 검증한다.
- DB 변경을 손으로 검증할 땐 **라운드트립/원복**(운영 데이터 보호, 로컬에서만).

## 주요 기능별 파일
- **검색/FTS**: `app/fts.py`(FTS5 색인, 파일명·태그·댓글·OCR·얼굴 라벨), `routes_photos.py`(통합 검색·날짜/GPS/텍스트 필터).
- **위치/지도**: `routes_photos.py`(지도 클러스터링·근처검색·추정 배지), `worker/location_estimator.py`(이웃 기반 GPS 추정), `js/panels/mapview.js`.
- **ML 파이프라인**: `worker_ml/{clip,yolo,faces,ocr,_ort}.py`, `worker/photo_work.py`(단계 진행).
- **얼굴 검색**: 라이트박스 얼굴 클릭 + 이미지 업로드 유사도 검색(`js/panels/lightbox.js`, `routes_photos.py`).
- **중복 제거**: `worker/dedup_cleanup.py`, `js/panels/duplicates.js`.
- **운영 CLI**: `app/tools/cutover.py`(루트 경로 재작성). 초기 백로그 GPU 색인→NAS 적재는 [docs/operations/bulk-index-gpu.md](docs/operations/bulk-index-gpu.md).

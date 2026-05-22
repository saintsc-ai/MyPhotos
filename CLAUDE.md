# MyPhotos — Claude Code Context

> Self-hosted photo catalog for ~10만 장 가족 사진. 기존 Synology Photos가
> Pentax MakerNote / iPhone HEIC / 일부 RAW를 못 다루는 한계를 극복하는 게 동기.

## 운영 환경

| | |
|---|---|
| Host | Synology DS3622xs+ (해놀로지, DSM 7.x, x86_64, Xeon D-1531) |
| Sibling 서비스 | `d2r-proxy.service` (8080 포트 점유) |
| NAS LAN IP | **192.168.1.201** |
| App 경로 | `/var/services/homes/scsung/myphotos` (= `/volume1/homes/scsung/myphotos`) |
| 실행 user | `scsung` (다른 서비스들과 컨벤션 일치) |
| 사진 root | `/volume1/photo` — 모두 777, scsung 소유, **read-only 정책** |
| Python | **3.11.9** (uv 관리) |
| Port | **8888** (8080은 d2r-proxy) |
| Git remote | `git@github.com:saintsc-ai/MyPhotos.git` (SSH-over-443 — DSM에서 22 blocked) |
| systemd | DSM 옛 버전 — `--now` / `MemoryMax` / `StandardOutput=append:` 미지원 |

## 기술 스택

- **Backend**: FastAPI + Uvicorn (`app.api.main:app`)
- **DB**: SQLite (WAL, FTS5/R-Tree 가용) + SQLAlchemy 2.0 + Alembic
- **Queue**: SQLite `jobs` 테이블 (UUID claim_token 패턴 — SQLite는 `SKIP LOCKED` 없음)
- **Worker**: 별도 프로세스, N개 스레드, `BackgroundScheduler` (APScheduler)
- **Image**: Pillow 12 + (optional) pillow-heif
- **External**: ExifTool (Pentax MakerNote/HEIC/RAW preview), ffmpeg (video frames)
- **Frontend**: 단일 정적 HTML + Leaflet/OSM (Google Maps 키 부담 회피)

## 디렉토리 / 책임

```
app/
├── api/           FastAPI 라우터 (main, deps, routes_photos)
├── admin/         관리 API (routes_roots, routes_jobs)
├── scanner/       파일시스템 디스커버리 (utils, discover)
├── worker/        잡 디스패처 (dispatcher, jobs, exif, thumbs, index_file, main)
├── web/static/    index.html (timeline + map 갤러리)
├── config.py      TOML 로더 (default + local merge)
├── db.py          SQLite 엔진 (WAL PRAGMA 자동 적용)
├── external.py    exiftool/ffmpeg 자동 검색 (config → vendor/<os-arch> → $PATH)
├── models.py      SQLAlchemy 모델
└── paths.py       PROJECT_ROOT 기준 경로 (data/, vendor/ 등)

alembic/           DB 마이그레이션 (0001_initial)
config/            default.toml (tracked) + local.toml (gitignored)
data/              ★ 런타임 전부 (catalog.db, thumbs/, logs/, trash/) — gitignored
scripts/           bootstrap.sh/.ps1, install-systemd.sh, install-vendor-linux-x64.sh
systemd/           myphotos-api.service.in, myphotos-worker.service.in
vendor/<os-arch>/  exiftool, ffmpeg 동봉 (gitignored except .gitkeep)
```

## 데이터 모델 (요지)

- `roots(id, label, abs_path, readonly, enabled, ...)` — **관리 UI에서 CRUD**.
  `label`은 안정 식별자, `abs_path`만 호스트마다 바뀜 → 포팅 시 PATCH로 끝.
- `photos(id, root_id, rel_path, sha256, taken_at, width/height, camera_*, exif_status, thumb_status, status, ...)`.
  **stage별 status 분리** — 한 단계 실패해도 행은 살림.
- `photo_locations(photo_id, latitude, longitude, altitude)` — GPS 별도 테이블 (대부분 사진엔 GPS 없으므로 메인 테이블 좁게 유지).
- `jobs(id, kind, payload, priority, status, claim_token, attempts, last_error, ...)`.

⚠️ **PK는 반드시 `Integer`** — SQLite는 `INTEGER PRIMARY KEY`만 ROWID alias로 자동증가. `BigInteger`(BIGINT) PK는 INSERT 시 NULL 실패 (학습 사항).

## 파이프라인

```
discover_root job
  └─ os.scandir 이터레이션 (메모리-안전, 미분류 폴더 73k 엔트리 대응)
  └─ NFC 정규화 + ignore (@eaDir 등) 적용
  └─ photos 행 upsert (size+mtime_ns signature) → 변경된 것만 index_file enqueue

index_file job
  1. 파일 존재 확인 (없으면 status='missing')
  2. SHA-256 (스트리밍)
  3. EXIF — chain = ["pillow", "exiftool"] (RAW/HEIC/video는 exiftool 우선)
  4. 썸네일 (sha256 기반 경로):
       - 일반 이미지   → Pillow + ImageOps.exif_transpose
       - HEIC          → pillow-heif가 Pillow에 등록되면 동일 경로
       - RAW           → ExifTool -b -JpgFromRaw/-PreviewImage 추출 후 Pillow scale
       - video         → ffmpeg 1-frame -> Pillow
  5. GPS → photo_locations 별도 upsert

매 단계 commit. 실패는 (exif|thumb)_status='failed' + error 기록.
```

## 주요 엔드포인트

| | |
|---|---|
| `GET /` | Static 갤러리 |
| `GET /healthz` | 상태 |
| `GET/POST/PATCH/DELETE /api/admin/roots[/{id}]` | 루트 CRUD |
| `POST /api/admin/roots/{id}/scan?limit=N` | 디스커버리 트리거 |
| `GET /api/admin/jobs/stats` | 큐 깊이 |
| `GET /api/admin/jobs/recent?status_filter=failed` | 실패 잡 확인 |
| `POST /api/admin/jobs/retry-photos` | failed 사진 재처리 (도구 추가 후) |
| `GET /api/photos?root_id=&date_from=&date_to=&page=` | 사진 목록 |
| `GET /api/photos/{id}/thumb?size=256\|1024` | 썸네일 |
| `GET /api/photos/{id}/original` | 원본 다운로드 |
| `GET /api/photos/locations?bbox=&limit=` | 지도 마커 |
| `GET /api/docs` | Swagger |

## 진행 상황 (commit 순)

- `379b514` MVP 1 — skeleton, models, API/worker entry, alembic, bootstrap
- `188fb19` git에 +x 비트 박음
- `a06091a → 5e8383f` pillow-heif 처리 (메인 → optional `[heic]`)
- `c42427a` systemd DSM 호환 (MemoryMax/append: 제거)
- `9b95481` API port 8080 → 8888
- `bc9d83b` API host 0.0.0.0 (LAN 노출, 인증 없음 전제)
- `bad5c4e` MVP 2 — scanner + dispatcher + EXIF/thumb pipeline + photos API
- `831ffbd → b781e42` PK BigInteger → Integer 수정 (SQLite autoincrement)
- `09e6b3c` 미니 갤러리 (timeline + Leaflet map)
- `07735e1` 포맷 커버리지 — RAW preview 추출, EXIF 체인 재정렬, retry 엔드포인트, vendor 설치 스크립트

## 검증 완료

- 200장 부분 스캔 통과 (queued→running→done, failed:0)
- iPhone JPG 인식 (`camera_model:"iPhone 16 Pro Max"`)
- 두 systemd 서비스 active

## 다음 할 일 (우선순위)

1. ⏳ **NAS에서 `./scripts/install-vendor-linux-x64.sh` 실행** → exiftool/ffmpeg `vendor/linux-x64/`에 설치
2. ⏳ **`uv pip install --python .venv/bin/python -e ".[heic]"` 재시도** — DSM glibc에서 wheel 잡히는지 확인 (실패 시 더 낮은 pillow-heif 버전 시도: 0.16~0.18)
3. ⏳ `sudo systemctl restart myphotos-worker` → 로그에서 `exiftool: ...`, `ffmpeg: ...` 감지 확인
4. ⏳ retry-photos로 실패한 200장 재처리하여 검증
5. ⏳ **풀스캔** (`POST /api/admin/roots/1/scan` — limit 없이) — 10만+장, 워커 6스레드 기준 수 시간
6. ⏸ 인증 (passlib → pwdlib 또는 bcrypt 직접; 가족 공유 활성화 전 필수)
7. ⏸ watchdog 실시간 감지 활성화 (현재는 daily APScheduler 풀스캔만)
8. ⏸ 검색 필터 UI (날짜 범위, 루트, 카메라, 텍스트)
9. ⏸ 외부 접근 전략 결정 (DSM 리버스 프록시 / Tailscale)
10. ⏸ Phase 2: `readonly` 풀고 이동/삭제 활성화. 휴지통은 `data/trash/`로

## 알려진 함정 (디버깅 빨리하려면)

| 함정 | 증상 | 해결 |
|---|---|---|
| SQLite `BigInteger` PK | `IntegrityError: NOT NULL` on insert | `Integer` 사용 |
| uv venv엔 pip 없음 | `ModuleNotFoundError: pip._internal` | `uv pip install ...` 또는 `uv pip install pip` |
| DSM 22 outbound 차단 | SSH timeout | `ssh.github.com:443` (~/.ssh/config) |
| DSM home 777 → SSH key | UNPROTECTED PRIVATE KEY | `chmod 700 ~/.ssh; chmod 600 ~/.ssh/id_*` |
| pillow-heif 1.x wheel 누락 | libheif 헤더 빌드 실패 | `<1.0` 핀 (그래도 실패하면 더 낮춰 시도) |
| DSM systemd 옛 버전 | `Unknown lvalue 'MemoryMax'` | 옵션 제거. journal로 로깅 |
| `systemctl --now` 미지원 | `unrecognized option` | `enable` + `start` 분리 |
| 한글 NFC/NFD | photos 중복 row | scanner/utils.nfc() 어디서나 적용 |
| Synology Photos `@eaDir` | 인덱싱 노이즈 | `config.scanner.ignore_dirs` 기본값에 포함 |
| 외부 LAN에서 `127.0.0.1` 바인딩 | `Connection refused` | systemd 유닛 `--host 0.0.0.0` |

## 포팅성 원칙 (절대 깨지 말 것)

1. **모든 런타임 상태는 `data/`에**. 다른 어디에도 쓰지 않는다 (썸네일/로그/DB/휴지통).
2. **루트 abs_path 외에 절대경로 박지 않는다**. `app/paths.py`의 `PROJECT_ROOT` 기준.
3. **DB 안의 `photos.rel_path`는 POSIX `/`로 정규화 + NFC**.
4. **사진 원본 폴더에 우리 파일을 만들지 않는다** (Synology Photos와 공존, 다른 NAS로 옮길 때 안전).
5. **vendor/ 동봉이 시스템 install보다 우선** — 새 NAS에서도 그대로 작동.
6. **MariaDB 후보였으나 SQLite 결정** — 포팅성 위해. 변경 금지 (변경 시 jobs 큐 claim_token 패턴 / R-Tree / migration 다 영향).

## 명령 치트시트

```bash
# NAS: 코드 갱신 + 재시작
cd ~/myphotos && git pull \
  && uv pip install --python .venv/bin/python -e . \
  && sudo systemctl restart myphotos-api myphotos-worker

# 외부 도구 설치 (한 번)
./scripts/install-vendor-linux-x64.sh
uv pip install --python .venv/bin/python -e ".[heic]"

# DB 새로 만들기 (스키마 변경 후 등 개발 단계)
sudo systemctl stop myphotos-api myphotos-worker
rm -f data/catalog.db data/catalog.db-wal data/catalog.db-shm
.venv/bin/python -m alembic upgrade head
sudo systemctl start myphotos-api myphotos-worker

# 운영 조작
curl -X POST "http://192.168.1.201:8888/api/admin/roots/1/scan?limit=200"
curl -s http://192.168.1.201:8888/api/admin/jobs/stats
curl -X POST http://192.168.1.201:8888/api/admin/jobs/retry-photos \
  -H "Content-Type: application/json" \
  -d '{"thumb_status":"failed","stages":["thumb"]}'

# 로그
sudo journalctl -u myphotos-api    -n 60 --no-pager
sudo journalctl -u myphotos-worker -f
```

## 사용자 프로필 (working with `scsung`)

- 한국어 소통. 답변도 한국어.
- 다른 서비스 운영 경험 있음 (mytrade, d2emuproxy 등 — `~/scsung/<svc>` 패턴).
- 도구 친화적. 명령어 그대로 실행 / 결과 붙여넣기로 진행.
- **secret을 채팅에 그대로 붙여넣은 적 있음** → 토큰/암호 받지 말고, 노출되면 즉시 revoke 안내.
- 결정 빠름. 옵션 제시하면 즉답하는 편.
- Pentax + iPhone (16/17) + 일반 디카 혼용. RAW + HEIC 둘 다 핵심 요구.

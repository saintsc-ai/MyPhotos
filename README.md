# MyPhotos

![MyPhotos map view](images/map.png)

> 한국어 / [English](#english)

직접 운영하는 사진 카탈로그. 메타데이터 인덱싱과 웹 브라우징을 지원합니다.

- **백엔드**: FastAPI + SQLite (WAL, FTS5, R-Tree)
- **워커 2개**: 인덱싱 워커(스캔/EXIF/썸네일) + ML 워커(객체 검출/CLIP 임베딩/얼굴 검출·클러스터링)
- **저장소**: 기존 사진 폴더는 읽기 전용으로 인덱싱. 썸네일과 DB는 `data/` 아래에 보관
- **자동 분류** (선택): YOLOv8(객체) + CLIP(주제/장면) + YuNet/SFace(얼굴) — 모두 ONNX, CPU 전용
- **대상 호스트**: Synology DSM (DS3622xs+, x86_64), systemd로 실행

## 디렉토리 구조

```text
myphotos/
├── app/                # 애플리케이션 코드
│   ├── api/            # FastAPI 앱 (uvicorn 엔트리)
│   ├── admin/          # 관리용 CRUD (roots, jobs, ml)
│   ├── worker/         # 스캐너 + 인덱싱 잡 러너 (systemd 엔트리)
│   ├── worker_ml/      # ML 잡 러너 — YOLO / CLIP / face (별도 systemd 엔트리)
│   └── web/            # HTMX 템플릿 / 정적 파일
├── config/
│   ├── default.toml    # 기본 설정 (커밋됨)
│   └── local.toml      # 호스트별 오버라이드 (커밋 안 됨)
├── data/               # 런타임 (커밋 안 됨) — DB, 썸네일, 모델, 로그, 휴지통
│   └── models/         # ONNX 모델 (yolo / clip / face) — install-ml-models.sh
├── vendor/             # OS별 바이너리 (exiftool, ffmpeg)
├── alembic/            # DB 마이그레이션
├── scripts/            # 부트스트랩, systemd 설치, ML 모델 다운로드/업로드
└── systemd/            # 유닛 템플릿 (api / worker / ml-worker)
```

## 설치

대상 환경별로 별도 가이드:

| 환경 | 가이드 |
| --- | --- |
| **Synology NAS** (DSM 7.x, systemd) | [docs/install/synology.md](docs/install/synology.md) |
| **Docker** (DSM Container Manager / Linux+Docker / Windows+Docker Desktop) | [docs/install/docker.md](docs/install/docker.md) |
| **일반 Linux** (Debian/Ubuntu/Fedora/Arch + systemd) | [docs/install/linux.md](docs/install/linux.md) |
| **Windows** (개발용) | [docs/install/windows.md](docs/install/windows.md) |

설치가 끝난 뒤의 운영은 주제별로 분리되어 있습니다 — 어느 환경(Synology / Linux / Windows)이든 동일하게 적용됩니다.

## 설치 후 운영

| 주제 | 가이드 |
| --- | --- |
| **일상 운영** — 코드 업데이트 / watcher / 백업 / 트러블슈팅 | [docs/operations/post-install.md](docs/operations/post-install.md) |
| **외부 DB (MariaDB / PostgreSQL)** — DSN 설정, 마이그레이션, 백업 | [docs/operations/external-db.md](docs/operations/external-db.md) |
| **다른 호스트로 이전** — NAS / Linux / Windows 간 (재인덱싱 없이) | [docs/operations/porting.md](docs/operations/porting.md) |

각 가이드는 Linux/Synology (systemd)와 Windows (`myphotos.ps1`) 명령을 함께 다룹니다.

## HTTPS 설정 (선택 — 권장)

기본은 `http://NAS:8888`로 접속합니다. 다음의 경우 HTTPS가 필요/권장됩니다:

- **외부(인터넷)에서 접속** — 비밀번호·세션 쿠키가 평문으로 흐르지 않게.
- **PWA(홈 화면 앱)의 오프라인 캐시(서비스워커)** — 보안 컨텍스트(HTTPS 또는
  `localhost`)에서만 동작합니다. 평문 `http://NAS:8888`에선 서비스워커가 등록되지
  않습니다. (단, 반응형 UI와 iOS "홈 화면에 추가" 전체화면 실행은 HTTP에서도 됩니다.)
- **지도·사진 GPS의 "현재 위치" 버튼** — 브라우저 위치 API(`navigator.geolocation`)도
  보안 컨텍스트 전용이라 HTTP에선 실패합니다. (사진을 EXIF GPS로 지도에 표시하거나
  지도 클릭으로 GPS를 편집하는 것은 HTTP에서도 정상 — *기기의 현재 위치* 따오기만 제한.)

방법 (택1):

**A. Synology DSM 리버스 프록시 + Let's Encrypt** — NAS만으로 (도메인/DDNS 필요)
1. 제어판 → 보안 → 인증서 → 추가 → Let's Encrypt 인증서 발급.
2. 제어판 → 로그인 포털 → 고급 → **리버스 프록시** → 생성:
   - 소스: `HTTPS` / `photos.example.com` / `443`
   - 대상: `HTTP` / `localhost` / `8888` (`.env`의 `API_PORT`)
3. 리버스 프록시 항목에 위 인증서를 지정 → `https://photos.example.com` 접속.

**B. Tailscale** — 도메인·포트 개방 불필요, 가장 간단 (내 기기끼리만 노출)
```bash
sudo tailscale serve --bg 8888      # https://<machine>.<tailnet>.ts.net → localhost:8888
```
MagicDNS + 자동 인증서로 HTTPS가 바로 됩니다. (Tailscale 버전에 따라 `tailscale serve`
문법이 다를 수 있음 — `tailscale serve status`로 확인.)

**C. Caddy / nginx 리버스 프록시** — 일반 Linux / Docker
Caddy 예 (`Caddyfile`): `photos.example.com { reverse_proxy localhost:8888 }`
→ Let's Encrypt 인증서 자동 발급·갱신.

> ⚠ **자가서명(self-signed) 인증서**는 브라우저가 신뢰하지 않아 경고가 뜨고
> **서비스워커도 동작하지 않습니다**. PWA 오프라인까지 원하면 위 A/B/C처럼
> 신뢰되는 인증서를 쓰세요. LAN 전용이면 `http://NAS:8888` 그대로도 무방합니다.

---

## English

Self-hosted photo catalog with metadata indexing and web browsing.

- **Backend**: FastAPI + SQLite (WAL, FTS5, R-Tree)
- **Two workers**: indexing (scanning / EXIF / thumbnails) and ML (object detection / CLIP embeddings / face detection + clustering), each as its own systemd unit
- **Storage**: indexes existing folders read-only; thumbnails and DB live inside `data/`
- **Auto-classification** (optional): YOLOv8 (objects) + CLIP (topics/scenes) + YuNet/SFace (faces) — all ONNX, CPU only
- **Target host**: Synology DSM (DS3622xs+, x86_64) via systemd

## Layout

```text
myphotos/
├── app/                # application code
│   ├── api/            # FastAPI app (uvicorn entry)
│   ├── admin/          # admin CRUD (roots, jobs, ml)
│   ├── worker/         # scanner + indexing job runner (systemd entry)
│   ├── worker_ml/      # ML job runner — YOLO / CLIP / face (separate systemd entry)
│   └── web/            # HTMX templates / static
├── config/
│   ├── default.toml    # built-in defaults (tracked)
│   └── local.toml      # per-host overrides (NOT tracked)
├── data/               # runtime (NOT tracked) — DB, thumbs, models, logs, trash
│   └── models/         # ONNX weights (yolo / clip / face) — install-ml-models.sh
├── vendor/             # OS-specific binaries (exiftool, ffmpeg)
├── alembic/            # DB migrations
├── scripts/            # bootstrap, systemd install, ML model download/upload
└── systemd/            # unit templates (api / worker / ml-worker)
```

## Install

Pick the guide that matches your environment:

| Environment | Guide |
| --- | --- |
| **Synology NAS** (DSM 7.x, systemd) | [docs/install/synology.md](docs/install/synology.md) |
| **Docker** (DSM Container Manager / Linux+Docker / Windows+Docker Desktop) | [docs/install/docker.md](docs/install/docker.md) |
| **Generic Linux** (Debian/Ubuntu/Fedora/Arch + systemd) | [docs/install/linux.md](docs/install/linux.md) |
| **Windows** (dev) | [docs/install/windows.md](docs/install/windows.md) |

Post-install ops are split by topic — they apply equally to every environment (Synology / Linux / Windows).

## Post-install

| Topic | Guide |
| --- | --- |
| **Day-to-day ops** — code update / watcher / backups / troubleshooting | [docs/operations/post-install.md](docs/operations/post-install.md) |
| **External DB (MariaDB / PostgreSQL)** — DSN setup, migration, backups | [docs/operations/external-db.md](docs/operations/external-db.md) |
| **Porting to a new host** — across NAS / Linux / Windows (no re-index) | [docs/operations/porting.md](docs/operations/porting.md) |

Each guide covers both Linux/Synology (systemd) and Windows (`myphotos.ps1`) commands.

## HTTPS (optional — recommended)

By default you reach MyPhotos at `http://NAS:8888`. HTTPS is needed/recommended when:

- **Accessing from the internet** — so passwords and session cookies don't travel in clear text.
- **PWA (home-screen app) offline caching (service worker)** — only works in a secure
  context (HTTPS, or `localhost`). A service worker will not register over plain
  `http://NAS:8888`. (Responsive UI and iOS "Add to Home Screen" full-screen launch
  still work over HTTP.)
- **The "use my location" buttons (map + photo GPS edit)** — the browser Geolocation
  API is also secure-context only, so it fails over HTTP. (Showing photos on the map by
  their EXIF GPS, and editing GPS by clicking the map, still work over HTTP — only
  reading the *device's current* location is restricted.)

Pick one:

**A. Synology DSM reverse proxy + Let's Encrypt** — NAS only (needs a domain / DDNS)
1. Control Panel → Security → Certificate → Add → issue a Let's Encrypt cert.
2. Control Panel → Login Portal → Advanced → **Reverse Proxy** → Create:
   - Source: `HTTPS` / `photos.example.com` / `443`
   - Destination: `HTTP` / `localhost` / `8888` (`API_PORT` from `.env`)
3. Assign that cert to the reverse-proxy host → browse `https://photos.example.com`.

**B. Tailscale** — no domain or port-forwarding, simplest (exposed only to your devices)
```bash
sudo tailscale serve --bg 8888      # https://<machine>.<tailnet>.ts.net → localhost:8888
```
MagicDNS + an automatic cert give you HTTPS immediately. (`tailscale serve` syntax varies
by version — check `tailscale serve status`.)

**C. Caddy / nginx reverse proxy** — generic Linux / Docker
Caddy example (`Caddyfile`): `photos.example.com { reverse_proxy localhost:8888 }`
→ Let's Encrypt cert issued and renewed automatically.

> ⚠ **Self-signed certs** aren't trusted by browsers (warning prompts) and **service
> workers won't run** behind them. For offline PWA use a trusted cert (A/B/C above).
> For LAN-only use, plain `http://NAS:8888` is fine.

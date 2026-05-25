# MyPhotos

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

## 설치 (Synology NAS — 단계별)

> 아래 명령들은 모두 `~`(현재 사용자의 홈)와 `$USER`(현재 사용자명)를
> 사용하므로, 어떤 DSM 계정으로 로그인했든 그대로 복사·실행하면 됩니다.
> DSM의 사용자 홈은 보통 `/var/services/homes/$USER`인데 셸의 `~`가
> 이를 자동으로 가리킵니다.
>
> 설치 폴더 이름(여기서는 `myphotos`)도 원하는 이름으로 바꾸셔도 됩니다 —
> 이하 명령에서 `~/myphotos` 부분만 그에 맞춰 바꾸세요.

### 사전 준비

| 항목 | 비고 |
| --- | --- |
| DSM 사용자 계정 | 어떤 ID든 OK. `sudo` 권한 필요 (systemd 유닛 설치 시) |
| SSH 접근 | DSM 제어판 → 터미널 및 SNMP → SSH 활성 |
| 인터넷 | uv / 의존성 / vendor 바이너리 다운로드용 |
| **Perl** | DSM 패키지 센터에서 "Perl" 설치 (exiftool이 Perl 스크립트). 미설치면 RAW/HEIC EXIF 추출 실패 |
| 사진 root 폴더 | 예: `/volume1/photo`. 사용자에게 읽기 권한 |
| 8888 포트 | 다른 서비스가 안 쓰면 그대로. 점유 시 [설정](#설치-후-운영) 참고 |

> **DSM에 Perl 설치하기**: 패키지 센터 → 모든 패키지 → "Perl" 검색 → 설치.
> Synology 공식 패키지라 안전합니다. 설치 후 SSH에서 `which perl`로 확인 —
> `/usr/bin/perl`이나 `/usr/local/bin/perl` 경로가 나오면 OK.

### 0) uv 설치 (1회만)

[uv](https://docs.astral.sh/uv/)는 Python 버전 + venv를 한 번에 관리하는 도구입니다.

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
source ~/.bashrc          # PATH 즉시 반영
uv python install 3.11.9  # 사용자 영역에 Python 3.11.9 설치
```

검증:
```bash
uv --version              # → uv 0.x.y
```

### 1) 코드 받기

```bash
cd ~
git clone https://github.com/saintsc-ai/MyPhotos.git myphotos
cd ~/myphotos
```

> 폴더 이름을 다르게 쓰고 싶다면 (예: `photo-server`) clone 끝의 인자를
> 바꾸세요: `git clone <URL> photo-server`. 이후 `~/photo-server`로 cd.

### 2) Python venv + 라이브러리 설치

```bash
./scripts/bootstrap.sh
```

스크립트가 자동으로:
- `.venv/`에 Python 3.11 가상환경 생성 (uv가 있으면 사용, 없으면 시스템 python)
- `pyproject.toml` 의존성 설치 (`fastapi`, `sqlalchemy`, `bcrypt`, `pillow` 등)

검증:
```bash
.venv/bin/python --version    # → Python 3.11.x
```

### 3) exiftool / ffmpeg 설치 (RAW / HEIC / 동영상 썸네일용)

```bash
./scripts/install-vendor-linux-x64.sh
```

`vendor/linux-x64/`에 두 바이너리가 들어갑니다. 시스템 PATH에 이미 있으면
이 단계는 건너뛰어도 되지만, 호스트 이전 시 같이 옮길 수 있어 편합니다.

> ⚠ **exiftool은 Perl 스크립트**라서 DSM에 Perl 패키지가 설치되어 있어야
> 합니다 (사전준비표 참고). `./vendor/linux-x64/exiftool -ver` 실행이
> `Can't locate ... in @INC` 같은 에러로 떨어지면 Perl 누락이 원인.

검증:
```bash
which perl                                 # /usr/bin/perl 또는 /usr/local/bin/perl
./vendor/linux-x64/exiftool -ver           # 숫자 (예: 12.85)
./vendor/linux-x64/ffmpeg -version | head -1
```

### 4) (선택) HEIC 직접 열기 활성화

iPhone HEIC를 Pillow로 직접 열어 더 빠르게 처리하고 싶을 때:
```bash
uv pip install --python .venv/bin/python -e ".[heic]"
```

설치 실패 시 (DSM glibc/wheel 호환 문제)는 그냥 넘겨도 됩니다 — exiftool이
HEIC 메타데이터/썸네일을 대신 처리합니다.

### 5) DB 스키마 생성

```bash
.venv/bin/python -m alembic upgrade head
```

`data/catalog.db` (SQLite)가 생성되고 모든 테이블이 만들어집니다. 처음에는
`0001` ~ 가장 최신 마이그레이션까지 순서대로 적용됩니다.

검증:
```bash
.venv/bin/python -m alembic current
# 출력 끝줄에 (head)가 있어야 OK — 예: 0005_tags_description (head)
```

> **업데이트할 때마다 이 단계 한 번 더 실행**하는 게 안전합니다. 새 컬럼이나
> 테이블 추가가 있었다면 자동 반영되고, 없으면 no-op.

### 6) (선택) 호스트별 설정

대부분의 값은 설치 후 관리 UI에서 변경 가능합니다 (관리 → 설정 탭).
지금 손댈 게 거의 없습니다만, 미리 바꾸고 싶다면:

```bash
[ -f config/local.toml ] || cp config/local.example.toml config/local.toml
# 편집기로 열어 수정 — 예시: 워커 동시성, 앱 이름, 시간대 등
```

`secret_key`는 첫 부팅 시 `data/session.secret`에 자동 생성됩니다.

### 7) (선택) ML 자동 분류 모델 다운로드

자동 분류(YOLO 객체 / CLIP 주제 / 얼굴 검출+클러스터)를 쓰려면 ONNX 가중치
6개(~140 MB)를 `data/models/`에 받아둡니다. 인증 없이 본 리포의 GitHub
Release에서 받아집니다:

```bash
./scripts/install-ml-models.sh
```

기대 결과:

```text
data/models/yolo/yolov8n.onnx
data/models/clip/{vision_quantized, text_quantized}.onnx
data/models/clip/tokenizer.json
data/models/face/{yunet, sface}.onnx
```

모델을 받지 않으면 인덱싱/EXIF/썸네일/검색은 그대로 동작하고, 자동 분류만
비활성 상태가 됩니다 (관리 페이지의 ML 카드도 빈 통계로 표시).

> **포크해서 쓰는 경우**: 본 리포의 Release URL이 fallback 없이 깨지면
> `MYPHOTOS_RELEASE_BASE=https://github.com/<your>/<repo>/releases/download/models-v1`
> 환경변수로 본인 Release를 가리키고, 모델을 한 번 받아 `scripts/upload-ml-models.sh`
> (gh CLI 필요)로 본인 Release에 업로드해두면 됩니다.

### 8) systemd 서비스 등록

```bash
./scripts/install-systemd.sh
```

스크립트가 현재 사용자(`$USER`)와 설치 경로(`$PWD`)를 자동으로 채워서
세 unit 파일을 `/etc/systemd/system/`에 설치합니다:
- `myphotos-api.service` — FastAPI (uvicorn) 8888 포트
- `myphotos-worker.service` — 스캐너 + 인덱싱 워커
- `myphotos-ml-worker.service` — ML 워커 (YOLO / CLIP / 얼굴). 7단계를
  스킵했으면 그냥 안 켜고 둬도 됩니다 — 잡이 안 들어오니 idle 상태로 머묾.

```bash
sudo systemctl enable myphotos-api myphotos-worker myphotos-ml-worker
sudo systemctl start  myphotos-api myphotos-worker myphotos-ml-worker
```

> DSM의 옛 systemd 빌드는 `--now` 옵션을 지원 안 해서 `enable`과 `start`를
> 두 줄로 분리했습니다. ML 워커는 `Nice=15` / `IOSchedulingPriority=7`로
> 낮은 우선순위라 인덱싱 진행 중에도 공존합니다.

검증:
```bash
sudo systemctl status myphotos-api       | head -3
sudo systemctl status myphotos-worker    | head -3
sudo systemctl status myphotos-ml-worker | head -3
# 셋 다 "Active: active (running)" 이어야 OK
```

### 9) 첫 로그인 & 사진 폴더 등록

1. 브라우저에서 `http://<NAS-IP>:8888` 접속 (예: `http://192.168.1.10:8888`)
2. **admin / admin** 로그인
3. 빨간 띠의 "지금 변경" 클릭 → 새 비밀번호 설정 (4자 이상)
4. 우상단 **관리** → **사진 폴더** 탭 → **새 폴더 추가**:
   - **라벨**: `family` (영숫자/`_`/`-`만)
   - **절대 경로**: 실제 사진 폴더 (예: `/volume1/photo`)
   - **읽기 전용**: 체크 권장 (스캐너가 원본 파일을 만지지 않음)
5. 추가된 행에서 **시험** 버튼 클릭 → 200장 샘플 색인이 큐에 등록됨
6. **색인** 탭에서 진행 상황 확인 (5초마다 자동 갱신). 실패한 잡이 0건이면
7. 다시 **사진 폴더** 탭 → 같은 행의 limit 입력은 비우고 **스캔** 버튼 → 풀스캔 시작
   - 10만 장 기준 NAS HDD에서 6~12시간 정도 소요

### 10) (선택) ML 자동 분류 시작

7단계에서 모델을 받았다면 관리 페이지 **ML 자동 분류** 카드에서 분류 잡을
큐에 등록합니다:

- **단계 체크박스** (`objects` / `embedding` / `faces`) — 처음에는 `objects`만
  켜고 limit 200 정도로 작게 시작해서 동작 확인. 정상이면 셋 다 켜고 풀스케일
- **force_reclassify** — 보통 OFF. 이미 `ok`인 사진을 다시 돌리고 싶을 때만 ON
- 진행은 같은 카드의 stats (classify_pending/ok/failed, auto_tag_count,
  clip_embedded, faces_detected, face_cluster_total/named) 또는
  `sudo journalctl -u myphotos-ml-worker -f` 로 확인

10만 장 기준 객체+CLIP+얼굴 세 단계 다 도는 데 약 반나절~하루. ML 워커는
낮은 우선순위(`Nice=15`)이므로 인덱싱과 공존 가능합니다.

### 11) (선택) 가족 사용자 추가

관리 → **사용자** 탭 → **새 사용자 추가**:
- 사용자명: `mom`, `dad` 등
- 비밀번호: 임의 설정
- 관리자 권한: 보통 X (보기·공유·태그·코멘트 가능, 삭제는 불가)

### 12) (선택) 외부 노출

기본은 LAN 전체 (`0.0.0.0:8888`). WAN에서 쓰려면:
- DSM 제어판의 **역방향 프록시** 룰로 HTTPS 도메인 → `localhost:8888`
- 또는 [Tailscale](https://tailscale.com) 등 VPN 메시

자체 세션 쿠키 인증이라 외부 LB가 그대로 통과해도 무방.

## 설치 후 운영

### 코드 업데이트

```bash
cd ~/myphotos && git pull
uv pip install --python .venv/bin/python -e .                            # 의존성 변경 시
.venv/bin/python -m alembic upgrade head                                 # 스키마 변경 시
sudo systemctl restart myphotos-api myphotos-worker myphotos-ml-worker   # 셋 다 안전
```

> 어떤 단계가 필요한지 헷갈리면 그냥 4줄 다 실행해도 안전합니다 — 변경
> 없으면 모두 no-op. ML 워커를 안 켰다면 `restart` 마지막 토큰은
> 빼셔도 됩니다 (없는 유닛 재시작 시 에러).

### 포트 변경

`config/local.toml`에:
```toml
[server]
port = 9000
```
그 후 `sudo systemctl restart myphotos-api`. 그리고 `myphotos-api.service`의
ExecStart에 포트가 박혀 있다면 `./scripts/install-systemd.sh` 재실행.

### 로그 보기
```bash
sudo journalctl -u myphotos-api    -n 60 --no-pager
sudo journalctl -u myphotos-worker -f
```

### 문제 해결

| 증상 | 확인 / 해결 |
| --- | --- |
| 타임라인이 비거나 500 오류 | `alembic current`가 `(head)`인지 확인. 아니면 `alembic upgrade head` 후 재시작 |
| 색인이 너무 느림 | 관리 → 설정 → 워커 → `concurrency` 조정. HDD면 3~4가 더 빠를 수 있음 |
| 워커 좀비 (status에 두 개 떠 있음) | `ps -ef \| grep app.worker`로 확인 후 systemd 외부 프로세스 `kill` |
| ML 워커가 active되자마자 죽음 | `journalctl -u myphotos-ml-worker -n 30`에 `model missing` 있으면 `./scripts/install-ml-models.sh` 미실행. 받은 후 재시작 |
| ML 분류 잡 다수가 failed | 모델 출력 형식이 코드 기대와 다른 변종일 수 있음. 위 로그의 traceback과 함께 이슈 등록 |
| admin 비밀번호 잊음 | `.venv/bin/python -c "from app.auth import hash_password; print(hash_password('새비번'))"` → 출력 해시를 sqlite3로 `UPDATE users SET password_hash='<해시>' WHERE username='admin';` |

## Docker 배포 (대안)

NAS에 Python/uv/exiftool/ffmpeg를 직접 설치하지 않고 컨테이너로 굴리고
싶을 때. 단일 이미지로 API + 인덱싱 워커 + (선택) ML 워커 3개 컨테이너를
띄웁니다.

### 0) 사전 준비

- Docker 20.10+ / Docker Compose v2 (DSM 7.2+는 "Container Manager"
  패키지에 둘 다 포함)
- 사진 폴더 경로(호스트 측) — 예: `/volume1/photo`
- runtime 데이터를 둘 호스트 경로 — 예: `/volume1/docker/myphotos/data`

### 1) 코드 받기 + 환경 파일 작성

```bash
cd ~
git clone https://github.com/saintsc-ai/MyPhotos.git myphotos
cd ~/myphotos
cp .env.example .env
# 편집: PHOTO_ROOT, DATA_DIR, API_PORT, APP_UID/APP_GID
```

- `PHOTO_ROOT` — 사진 폴더 절대 경로 (컨테이너에서 `/photos:ro`로 마운트됨)
- `DATA_DIR`   — 카탈로그 DB / 썸네일 / 로그가 들어갈 호스트 경로
- `APP_UID/GID` — 호스트에서 사진 파일을 소유한 계정의 `id -u` / `id -g`.
  이걸 안 맞추면 `/photos` 읽기 실패 또는 `/app/data` 쓰기 실패가 생깁니다.

### 2) 이미지 받기 + 실행

기본값은 GHCR에 미리 빌드된 `ghcr.io/saintsc-ai/myphotos:latest`를 pull하므로
NAS에서 빌드할 필요가 없습니다:

```bash
docker compose pull
docker compose up -d
```

API와 인덱싱 워커 2개 컨테이너가 뜹니다. ML 자동 분류까지 쓰려면:

```bash
docker compose --profile ml up -d         # ml-worker 추가 기동
docker compose exec ml-worker ./scripts/install-ml-models.sh   # 모델 ~140MB
docker compose restart ml-worker
```

> **로컬 코드로 빌드하고 싶다면**: `.env`에 `IMAGE=myphotos:dev` 추가 후
> `docker compose up -d --build`. 워크플로(`.github/workflows/docker.yml`)는
> main 푸시 / 태그 푸시(`v*.*.*`) / 수동 실행 시 `latest`, `sha-xxxxxxx`,
> 그리고 (태그 push인 경우) `vX.Y.Z` 태그로 GHCR에 자동 push합니다.

### 3) 로그 / 상태

```bash
docker compose ps
docker compose logs -f api worker
docker compose logs -f ml-worker          # ml profile 켰을 때
```

### 4) 업데이트

main에 새 커밋이 푸시되면 GHCR의 `latest` 태그가 갱신됩니다. NAS에서는:

```bash
docker compose pull
docker compose up -d                      # 변경된 컨테이너만 재기동
```

`git pull`은 docker-compose.yml/.env 같은 호스트 파일이 바뀌었을 때만
필요합니다. `alembic upgrade head`는 api 컨테이너 시작 시 자동 실행되므로
별도 수동 마이그레이션 불필요. 워커들은 api 컨테이너가 healthy(=마이그레이션
완료)될 때까지 기다렸다가 시작합니다.

### 5) 다른 호스트로 이전

- `DATA_DIR` 경로 통째로 + `config/local.toml`만 새 호스트에 옮기고
  같은 절차를 반복하면 됩니다 (재인덱싱 없음).
- DSM ↔ Linux ↔ Windows 호스트 간 이전도 동일. `roots.abs_path`만 새
  호스트의 컨테이너 내부 경로 (`/photos`)에 맞게 관리 UI에서 한 번 갱신.

### 동작 메모

| 항목 | 값 |
| --- | --- |
| 베이스 이미지 | `python:3.11-slim-bookworm` (onnxruntime 1.16 wheel 호환 위해 3.11 고정) |
| 외부 도구 | exiftool / ffmpeg / libheif1 → apt 설치 (vendor/ 불필요) |
| HEIC | pillow-heif 포함 빌드 |
| 컨테이너 사용자 | `myphotos` (UID/GID는 `--build-arg`로 조정 가능 — .env의 `APP_UID/GID`) |
| PID 1 | tini — SIGTERM이 uvicorn/워커까지 그대로 전달 |
| 헬스체크 | `GET /healthz` — 워커가 api healthy를 기다림 |
| 사진 폴더 | 컨테이너 안에서 `/photos`, **read-only** 바인드 |
| 런타임 상태 | 컨테이너 안에서 `/app/data` (호스트의 `DATA_DIR`) |
| 마이그레이션 | api 컨테이너 시작 시 자동 (`alembic upgrade head`) |

> ⚠ **사진 폴더 경로 등록**: 관리 UI에서 root를 추가할 때 절대 경로는
> 호스트 경로(`/volume1/photo`)가 아니라 **컨테이너 안 경로
> (`/photos`)** 를 입력하세요. 다른 호스트로 옮기든 컨테이너 안에서는
> 항상 `/photos`로 보입니다.

## 부트스트랩 (Windows 개발 환경)

```powershell
cd $env:USERPROFILE
git clone https://github.com/saintsc-ai/MyPhotos.git myphotos
cd myphotos
.\scripts\bootstrap.ps1
Copy-Item config\local.example.toml config\local.toml -ErrorAction SilentlyContinue
.\.venv\Scripts\python -m alembic upgrade head
.\scripts\run-api.ps1     # 한 터미널
.\scripts\run-worker.ps1  # 다른 터미널
```

→ `http://localhost:8888` 접속, admin/admin 로그인.

## 다른 호스트로 이전 (재인덱싱 없이)

다른 NAS로 이전해도 **재인덱싱 없이** 그대로 사용 가능합니다. 썸네일은
SHA-256으로 주소되고, `photos.rel_path`는 root 기준 상대 경로(POSIX/NFC)로
저장되어 있어 호스트별로 바뀌는 건 `roots.abs_path` 하나뿐입니다.

### 1) 원본 호스트 — 정합성 있는 스냅샷

```bash
sudo systemctl stop myphotos-api myphotos-worker
sqlite3 ~/myphotos/data/catalog.db ".backup ~/myphotos/data/catalog.db.snapshot"
```

> WAL 모드라 서비스 정지 없이 그대로 `data/`를 복사하면
> `catalog.db-wal`이 어중간한 상태일 수 있습니다. 위처럼 정지 → backup
> 한 번 → 전송이 안전합니다.

### 2) 새 호스트로 전송

`data/` 통째로 + `config/local.toml` 두 가지만 옮기면 됩니다.

```bash
# 환경에 맞게 두 변수 채우기
NEW_HOST="newnas.local"          # 새 NAS 주소 (또는 IP)
NEW_USER="$USER"                 # 새 NAS 쪽 사용자명 (보통 같은 ID)

# data/ 전체 (catalog.db, thumbs/, session.secret, trash/, logs/)
rsync -aP ~/myphotos/data/ \
  "$NEW_USER@$NEW_HOST:~/myphotos/data/"

# 호스트별 설정 (secret_key 포함 — 같은 키를 가져가면 기존 세션도 유지)
rsync -aP ~/myphotos/config/local.toml \
  "$NEW_USER@$NEW_HOST:~/myphotos/config/local.toml"
```

### 3) 새 호스트 — 셋업

```bash
# 코드는 새로 clone (vendor/와 .venv는 OS별이므로 재생성)
git clone git@github.com:saintsc-ai/MyPhotos.git ~/myphotos

# data/ 와 config/local.toml은 위 2)에서 이미 자리잡고 있음
cd ~/myphotos
./scripts/bootstrap.sh                       # Python venv
./scripts/install-vendor-linux-x64.sh        # exiftool / ffmpeg (OS별 바이너리)
./scripts/install-systemd.sh
sudo systemctl enable myphotos-api myphotos-worker
sudo systemctl start  myphotos-api myphotos-worker
```

### 4) 사진 폴더 경로 갱신

원본 NAS에서 `/volume1/photo`였던 root가 새 호스트에서는
`/mnt/data/photos`처럼 바뀌었을 수 있습니다. 관리 페이지에서 수정:

1. 브라우저로 `http://새-호스트:8888/admin.html` 접속
2. **사진 폴더** 탭 → 해당 루트 행의 **`경로`** 버튼 클릭
3. 새 절대 경로 입력 → 저장

루트의 **라벨은 그대로 유지**되고, `photos.rel_path`(상대 경로)도 그대로이므로
이 한 가지만 바꾸면 모든 사진이 다시 연결됩니다.

또는 curl로:

```bash
curl -b cookies -X PATCH http://newnas:8888/api/admin/roots/1 \
  -H "Content-Type: application/json" \
  -d '{"abs_path":"/mnt/data/photos"}'
```

### 5) 검증

관리 → **색인** 탭에서 EXIF/썸네일 진행률이 이전 NAS의 값과 동일한지 확인.
만약 일부가 `missing`으로 바뀌었다면 그건 root 안 내부 폴더 구조가
달라진 사진들 — 디스커버리를 한 번 돌리면(`시험` 버튼) `missing` 또는
`active`로 재정리됩니다.

### 옮기지 않는 것

| 항목 | 이유 |
| --- | --- |
| `vendor/<os-arch>/` | exiftool/ffmpeg는 OS별 바이너리. 새 호스트에서 `install-vendor-*.sh`로 재설치 |
| `.venv/` | Python venv도 호스트별. `bootstrap.sh`가 새로 만듦 |
| `*.db-wal`, `*.db-shm` | WAL 부속 파일은 backup 명령 이후 자동 흡수됨 |

### 옮기지 않으면 일어나는 일

| 빠뜨림 | 결과 |
| --- | --- |
| `data/catalog.db` | 전부 재색인 (몇 시간) |
| `data/thumbs/` | DB는 살아있지만 모든 썸네일 재생성 |
| `data/session.secret` | 새 키 자동 생성 → 모든 사용자 재로그인 |
| `config/local.toml` | 기본값으로 동작 (secret_key는 자동 생성). 별도 튜닝은 다시 설정 |

DB는 단일 SQLite 파일이며, 외부 서비스는 필요 없습니다.

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

## Install (Synology NAS — step by step)

> All commands use `~` (current user's home) and `$USER` (current user's
> name), so they work for any DSM account — no need to substitute a
> username. The DSM home directory is normally
> `/var/services/homes/$USER`, which `~` resolves to automatically.
>
> The install folder name (`myphotos` below) is also arbitrary — use a
> different name if you prefer; just replace `~/myphotos` accordingly.

### Prerequisites

| Item | Notes |
| --- | --- |
| DSM user account | Any login; needs `sudo` for systemd unit install |
| SSH access | DSM Control Panel → Terminal & SNMP → enable SSH |
| Internet | for uv / dependencies / vendor binary downloads |
| **Perl** | Install from DSM Package Center ("Perl"). The bundled exiftool is a Perl script; without Perl, RAW/HEIC EXIF extraction fails |
| Photo root folder | e.g. `/volume1/photo`, readable by the user |
| Port 8888 free | otherwise see [post-install](#post-install) |

> **Installing Perl on DSM**: Package Center → All Packages → search
> "Perl" → install. It's an official Synology package. Verify over SSH
> with `which perl` — `/usr/bin/perl` or `/usr/local/bin/perl` means
> it's available.

### 0) Install uv (one time)

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
source ~/.bashrc
uv python install 3.11.9
```

### 1) Clone the repo

```bash
cd ~
git clone https://github.com/saintsc-ai/MyPhotos.git myphotos
cd ~/myphotos
```

### 2) Python venv + dependencies

```bash
./scripts/bootstrap.sh
```

### 3) exiftool / ffmpeg (RAW / HEIC / video thumbnails)

```bash
./scripts/install-vendor-linux-x64.sh
```

> ⚠ **exiftool is a Perl script**, so Perl must be installed on DSM
> (see the prerequisites table). An error like
> `Can't locate ... in @INC` from `./vendor/linux-x64/exiftool -ver`
> means Perl is missing.

Verify:
```bash
which perl                                 # /usr/bin/perl or /usr/local/bin/perl
./vendor/linux-x64/exiftool -ver           # a version number
./vendor/linux-x64/ffmpeg -version | head -1
```

### 4) (optional) Native HEIC reader

```bash
uv pip install --python .venv/bin/python -e ".[heic]"
```

Skip silently on glibc/wheel mismatch — exiftool will handle HEIC.

### 5) Create / upgrade the DB schema

```bash
.venv/bin/python -m alembic upgrade head
```

Verify with `.venv/bin/python -m alembic current` — should end with
`(head)`. **Re-run this step on every code update** to pick up new
columns/tables; it's a no-op when nothing changed.

### 6) (optional) Host overrides

Most settings are editable later in the admin UI (관리 → 설정). If you
want to seed values up-front:

```bash
[ -f config/local.toml ] || cp config/local.example.toml config/local.toml
```

`secret_key` is auto-generated to `data/session.secret` on first boot.

### 7) (optional) Download ML classification models

Auto-classification (YOLO objects / CLIP topics / face detection + cluster)
needs six ONNX weights (~140 MB) in `data/models/`. Fetched from this
repo's GitHub Release without authentication:

```bash
./scripts/install-ml-models.sh
```

Skip silently and only indexing / EXIF / thumbnails / search will run; the
admin ML card will show empty counters.

> **Forking**: if the upstream Release URL ever goes away, point
> `MYPHOTOS_RELEASE_BASE` at your own fork's Release and use
> `scripts/upload-ml-models.sh` (requires `gh` CLI) to seed it.

### 8) Install systemd units

```bash
./scripts/install-systemd.sh        # fills $USER + $PWD into the templates
sudo systemctl enable myphotos-api myphotos-worker myphotos-ml-worker
sudo systemctl start  myphotos-api myphotos-worker myphotos-ml-worker
```

Three units are installed: `myphotos-api`, `myphotos-worker` (scanner +
indexing), `myphotos-ml-worker` (YOLO/CLIP/face). If you skipped step 7,
either leave the ML unit stopped or simply don't enqueue ML jobs — the
worker will idle.

> DSM ships an older systemd that doesn't accept `--now`, so `enable`
> and `start` are split. The ML worker runs at `Nice=15` /
> `IOSchedulingPriority=7` so it coexists with the indexing worker.

### 9) First login + photo root

1. Open `http://<NAS-IP>:8888`
2. Sign in with **admin / admin**
3. Use the "지금 변경" prompt to set a real password (≥ 4 chars)
4. Top-right **관리 (Admin)** → **사진 폴더 (Roots)** → **새 폴더 추가**:
   - Label: `family` (alphanumerics, `_`, `-`)
   - Absolute path: your photo folder (e.g. `/volume1/photo`)
   - Read-only: checked (recommended; scanner won't touch originals)
5. On the new row, **시험 (Sample, 200 photos)** for a smoke test
6. Watch **색인 (Indexing)** tab for progress (auto-refreshes every 5s)
7. Back to **사진 폴더**, **스캔 (Scan)** with no limit for a full run

### 10) (optional) Trigger auto-classification

If step 7 was done, open the admin **ML 자동 분류 (ML auto-classify)**
card and enqueue jobs:

- Stage checkboxes (`objects` / `embedding` / `faces`) — start with just
  `objects` and limit 200 to smoke-test, then enable all three at full scale.
- `force_reclassify` — usually off; only on when you want to redo
  already-classified photos.
- Progress shows live in the same card (classify_pending/ok/failed,
  auto_tag_count, clip_embedded, faces_detected, face_cluster_total/named)
  or via `sudo journalctl -u myphotos-ml-worker -f`.

100k photos through all three stages: roughly half a day to a day on CPU.

### 11) (optional) Add family users

관리 → **사용자 (Users)** → 새 사용자 추가. Leave "관리자" unchecked for
non-admin accounts that can browse / share / tag / comment but not delete.

### 12) (optional) Expose externally

DSM Reverse Proxy → `localhost:8888`, or wrap the host with Tailscale.
Session cookies pass through any standard LB.

> Note: uv-created venvs don't include `pip`. Use `uv pip install ...` for
> ad-hoc installs, or `.venv/bin/python -m <module>` to run scripts.

## Post-install

### Updating the code

```bash
cd ~/myphotos && git pull
uv pip install --python .venv/bin/python -e .       # if deps changed
.venv/bin/python -m alembic upgrade head            # if schema changed
sudo systemctl restart myphotos-api myphotos-worker
```

Running all four lines is always safe — they no-op when nothing changed.

### Troubleshooting

| Symptom | Check / fix |
| --- | --- |
| Empty timeline or 500 errors | `alembic current` should end in `(head)`; if not, `alembic upgrade head` and restart |
| Slow indexing | 관리 → 설정 → worker → `concurrency`. HDD storage often goes faster at 3–4 than 6+ |
| Two worker processes (status shows it) | `ps -ef \| grep app.worker`; `kill` any not under systemd |
| Forgot admin password | `.venv/bin/python -c "from app.auth import hash_password; print(hash_password('new_pw'))"`, then `sqlite3 data/catalog.db "UPDATE users SET password_hash='<hash>' WHERE username='admin';"` |

## Docker deployment (alternative)

Skip the Python / uv / exiftool / ffmpeg install on the NAS and run
everything as containers. One image, three containers (API + indexing
worker + optional ML worker).

### 0) Prerequisites

- Docker 20.10+ / Docker Compose v2 (DSM 7.2+ ships both in the
  "Container Manager" package)
- Host path to the photo library — e.g. `/volume1/photo`
- Host path for runtime data — e.g. `/volume1/docker/myphotos/data`

### 1) Clone + create the env file

```bash
cd ~
git clone https://github.com/saintsc-ai/MyPhotos.git myphotos
cd ~/myphotos
cp .env.example .env
# edit: PHOTO_ROOT, DATA_DIR, API_PORT, APP_UID/APP_GID
```

- `PHOTO_ROOT` — absolute path of your photo library (mounted at
  `/photos:ro` in containers).
- `DATA_DIR` — where the catalog DB, thumbnails, and logs live on the host.
- `APP_UID / APP_GID` — `id -u` / `id -g` of the host account that owns
  the photo files. If they don't match, reads on `/photos` or writes on
  `/app/data` will fail.

### 2) Pull the image + start

The default image is `ghcr.io/saintsc-ai/myphotos:latest`, prebuilt by GitHub
Actions — no local build needed on the NAS:

```bash
docker compose pull
docker compose up -d
```

This brings up the API and indexing worker. For ML auto-classification:

```bash
docker compose --profile ml up -d
docker compose exec ml-worker ./scripts/install-ml-models.sh   # ~140 MB
docker compose restart ml-worker
```

> **To build from your local tree instead**: set `IMAGE=myphotos:dev` in
> `.env`, then `docker compose up -d --build`. The workflow at
> `.github/workflows/docker.yml` publishes `latest`, `sha-xxxxxxx`, and (on
> tag pushes) `vX.Y.Z` images to GHCR on every main push, tag push, and
> manual dispatch.

### 3) Logs / status

```bash
docker compose ps
docker compose logs -f api worker
docker compose logs -f ml-worker          # when ml profile is up
```

### 4) Updating

GHCR's `latest` tag advances whenever main is pushed. On the NAS:

```bash
docker compose pull
docker compose up -d
```

`git pull` is only needed if `docker-compose.yml` / `.env` themselves
changed. `alembic upgrade head` runs automatically on each API container
start; the worker and ml-worker `depends_on` the API healthcheck, so they
don't start until migrations are applied.

### 5) Moving to a different host

Copy the `DATA_DIR` directory and `config/local.toml` to the new host and
repeat the steps above (no re-indexing). DSM ↔ Linux ↔ Windows moves all
work the same way — only `roots.abs_path` needs to be re-set in the admin
UI to whatever path the container sees (still `/photos` if you keep the
default bind).

### Notes

| Item | Value |
| --- | --- |
| Base image | `python:3.11-slim-bookworm` (pinned to match onnxruntime 1.16 wheels) |
| External tools | exiftool / ffmpeg / libheif1 via apt (no vendor/ needed) |
| HEIC | pillow-heif built into the image |
| Container user | `myphotos` (UID/GID tuned via build args / `.env`) |
| PID 1 | tini — SIGTERM propagates to uvicorn / workers |
| Healthcheck | `GET /healthz` — workers wait for the API to report healthy |
| Photo folder | `/photos` inside containers, **read-only** bind |
| Runtime state | `/app/data` inside containers (your `DATA_DIR` on the host) |
| Migrations | Run automatically on API container start |

> ⚠ When you add a root in the admin UI, the absolute path must be the
> **in-container** path (`/photos`), not the host path (`/volume1/photo`).
> The container always sees the bind target, regardless of host OS.

## Bootstrap (Windows dev)

```powershell
.\scripts\bootstrap.ps1
Copy-Item config\local.example.toml config\local.toml
.\scripts\run-api.ps1     # in one terminal
.\scripts\run-worker.ps1  # in another
```

## Porting to a new host (without re-indexing)

Moving the catalog to a different NAS keeps every photo's index intact —
**no re-indexing required**. Thumbnails are addressed by SHA-256 and
`photos.rel_path` is stored as a POSIX/NFC path relative to the root,
so the only host-specific value is `roots.abs_path`.

### 1) Source host — consistent snapshot

```bash
sudo systemctl stop myphotos-api myphotos-worker
sqlite3 ~/myphotos/data/catalog.db ".backup ~/myphotos/data/catalog.db.snapshot"
```

> WAL mode means a hot-copy of `data/` may include a half-written
> `catalog.db-wal`. Stopping the services first (or using `.backup`)
> avoids that.

### 2) Transfer to the new host

Two trees to copy: the whole `data/` directory and `config/local.toml`.

```bash
# Fill in these two for your environment
NEW_HOST="newnas.local"          # new NAS address (or IP)
NEW_USER="$USER"                 # account on the new NAS (often same)

# Runtime state — DB, thumbnails, session secret, trash, logs
rsync -aP ~/myphotos/data/ \
  "$NEW_USER@$NEW_HOST:~/myphotos/data/"

# Host config — same secret_key keeps existing sessions valid
rsync -aP ~/myphotos/config/local.toml \
  "$NEW_USER@$NEW_HOST:~/myphotos/config/local.toml"
```

### 3) New host — set up

```bash
# Fresh checkout (vendor/ and .venv are OS-specific, regenerated below)
git clone git@github.com:saintsc-ai/MyPhotos.git ~/myphotos

# data/ and config/local.toml are already in place from step 2.
cd ~/myphotos
./scripts/bootstrap.sh                       # Python venv
./scripts/install-vendor-linux-x64.sh        # exiftool / ffmpeg
./scripts/install-systemd.sh
sudo systemctl enable myphotos-api myphotos-worker
sudo systemctl start  myphotos-api myphotos-worker
```

### 4) Point the root at the new path

The old `/volume1/photo` likely lives at a different mount point on the
new host (e.g. `/mnt/data/photos`). Update it via the admin UI:

1. Open `http://new-host:8888/admin.html`
2. **사진 폴더 (Photo folders)** tab → click **`경로`** on the root row
3. Enter the new absolute path → save

The label stays the same and every photo's `rel_path` (relative path)
is unchanged, so this single edit reconnects everything.

Or via curl:

```bash
curl -b cookies -X PATCH http://newnas:8888/api/admin/roots/1 \
  -H "Content-Type: application/json" \
  -d '{"abs_path":"/mnt/data/photos"}'
```

### 5) Verify

Admin → **색인 (Indexing)** tab — the EXIF and thumbnail progress
counters should match the source host. If a subset has flipped to
`missing`, those are photos whose path within the root changed; a
discover run (sample-scan button on the root) will reconcile them
to `active` or `missing` again.

### Things NOT to copy

| Item | Why |
| --- | --- |
| `vendor/<os-arch>/` | exiftool/ffmpeg are OS-specific. Re-install via `install-vendor-*.sh` |
| `.venv/` | Python venv is host-specific. `bootstrap.sh` rebuilds it |
| `*.db-wal`, `*.db-shm` | WAL side files are absorbed by `.backup` |

### What happens if you skip a piece

| Missed | Consequence |
| --- | --- |
| `data/catalog.db` | Full re-index (several hours) |
| `data/thumbs/` | DB intact, every thumbnail regenerates |
| `data/session.secret` | New key auto-generated → every user must log in again |
| `config/local.toml` | Defaults take over (secret auto-generated); custom tuning lost |

The DB is a single SQLite file. No external services required.

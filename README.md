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
| **Git** | DSM 패키지 센터에서 "Git Server" 설치 (코드 clone에 필요). 미설치면 `git clone` 단계가 `command not found`로 실패 |
| **Perl** | DSM 패키지 센터에서 "Perl" 설치 (exiftool이 Perl 스크립트). 미설치면 RAW/HEIC EXIF 추출 실패 |
| 사진 root 폴더 | 예: `/volume1/photo`. 사용자에게 읽기 권한 |
| 8888 포트 | 다른 서비스가 안 쓰면 그대로. 점유 시 [설정](#설치-후-운영) 참고 |

> **DSM에 Git 설치하기**: 패키지 센터 → 모든 패키지 → "Git Server" 검색 → 설치.
> Synology 공식 패키지이고, "Git Server"라는 이름이지만 git CLI(`git` 명령)도
> 함께 들어옵니다. 설치 후 SSH에서 `git --version` 으로 확인 — 버전이 찍히면 OK.
> `command not found`로 떨어지면 PATH에 안 잡힌 것 —
> `/var/packages/Git/target/bin/git`이 실제 경로니까
> `export PATH="/var/packages/Git/target/bin:$PATH"`를 `~/.profile`에 추가하세요.

<!-- -->

> **DSM에 Perl 설치하기**: 패키지 센터 → 모든 패키지 → "Perl" 검색 → 설치.
> Synology 공식 패키지라 안전합니다. 설치 후 SSH에서 `which perl`로 확인 —
> `/usr/bin/perl`이나 `/usr/local/bin/perl` 경로가 나오면 OK.

### 0) uv 설치 (1회만)

[uv](https://docs.astral.sh/uv/)는 Python 버전 + venv를 한 번에 관리하는 도구입니다.
**일반 사용자 계정**으로 로그인한 상태에서 설치하세요:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
# 새로 만든 DSM 사용자는 ~/.bashrc 가 없는 경우가 많아 uv 설치
# 스크립트가 PATH 라인을 적어둘 파일을 못 찾습니다. 직접 추가:
echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.bashrc
source ~/.bashrc          # PATH 즉시 반영
uv python install 3.11.9  # 사용자 영역에 Python 3.11.9 설치
```

검증:
```bash
uv --version              # → uv 0.x.y
which uv                  # /var/services/homes/<user>/.local/bin/uv
```

> ⚠️ **`root`로 로그인하지 마세요.** DSM에서 `/root`는 시스템 업데이트
> 시 임시 공간으로 쓰이는 영역이라 비워둬야 하는데, uv 기본 설치 위치인
> `~/.local/bin`이 `/root/.local/bin`으로 잡혀 약 60MB가 그쪽에 쌓입니다.
>
> 정말 root로만 작업해야 하는 경우엔 명시적으로 위치를 지정하세요:
> ```bash
> mkdir -p /volume1/scripts/bin
> UV_INSTALL_DIR=/volume1/scripts/bin sh -c "$(curl -LsSf https://astral.sh/uv/install.sh)"
> echo 'export PATH="/volume1/scripts/bin:$PATH"' >> ~/.bashrc
> source ~/.bashrc
> ```

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
   - **읽기 전용**: 체크하면 **삭제·이동 작업이 서버에서 거부**됩니다.
     - 스캐너/EXIF/썸네일은 원래 read-only(원본 폴더에 쓰지 않음) 라
       이 플래그와 무관하게 그대로 동작
     - 라이트박스 🗑, 중복 popover "이것 빼고 휴지통", 관리 → 중복의
       자동정리는 모두 readonly 폴더 사진을 건너뜀 (개별 삭제 시도 시
       409, 일괄 작업 시 응답에 `skipped_readonly` 항목으로 리포트)
     - 원본 폴더를 안전망으로 두고 카탈로그만 관리하고 싶을 때 유용.
       실제로 사진을 옮기거나 지울 일이 있으면 관리 화면에서 임시로
       체크 해제 → 작업 → 다시 체크.
5. 추가된 행에서 **시험** 버튼 클릭 → 200장 샘플 색인이 큐에 등록됨
6. **색인** 탭에서 진행 상황 확인 (5초마다 자동 갱신). 실패한 잡이 0건이면
7. 다시 **사진 폴더** 탭 → 같은 행의 limit 입력은 비우고 **스캔** 버튼 → 풀스캔 시작
   - 10만 장 기준 NAS HDD에서 6~12시간 정도 소요

> ⚠ **사진 폴더 권한** — root 추가 직후 상태가 `접근 불가`로 뜨면 폴더
> 권한 문제입니다. Synology Photos가 만든 `/volume1/photo`는 보통
> `d---------+` (ACL 전용)이라 systemd가 실행하는 `$USER` 계정 권한으로는
> 읽을 수 없습니다. 호스트에서 한 번:
>
> ```bash
> ls -la /volume1/photo                # d---------+ 인지 확인
> sudo chmod 777 /volume1/photo        # 또는 폴더 별로 chmod -R 755
> ```
>
> Synology Photos는 권한 변경에 영향받지 않고 계속 동작합니다. 읽기 전용
> 옵션을 켰다면 MyPhotos도 원본을 절대 수정하지 않으므로 안전합니다.
> 더 정교하게 가려면 ACL로 `$USER`만 read 추가:
>
> ```bash
> sudo synoacltool -add /volume1/photo "user:$USER:allow:r-x---a-R-c--:fd--"
> ```

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

가장 안전한 한 줄 — 모든 단계를 순서대로 실행하고, 변경이 없는 단계는
no-op이므로 매번 그대로 써도 부작용 없습니다:

```bash
cd ~/myphotos && git pull \
  && uv pip install --python .venv/bin/python -e . \
  && .venv/bin/python -m alembic upgrade head \
  && sudo systemctl restart myphotos-api myphotos-worker myphotos-ml-worker myphotos-watcher
```

활성화하지 않은 유닛이 있으면 그 토큰은 빼세요 — 존재하지 않는 유닛
재시작 시 에러. (예: ML 워처/watcher 안 켰으면 `myphotos-api myphotos-worker`만)

#### 단계별로 (각 단계가 언제 필요한지)

| 단계 | 명령 | 필요한 때 |
| --- | --- | --- |
| 1. 코드 받기 | `git pull` | 항상 |
| 2. 의존성 동기화 | `uv pip install --python .venv/bin/python -e .` | `pyproject.toml` 변경 시 (새 라이브러리/버전 핀 등) |
| 3. DB 마이그레이션 | `.venv/bin/python -m alembic upgrade head` | `alembic/versions/` 에 새 파일 추가 시 |
| 4. 서비스 재시작 | `sudo systemctl restart myphotos-api myphotos-worker myphotos-ml-worker` | 코드/설정/스키마 어떤 것이든 바뀌었으면 |

확인 — 어떤 단계가 진짜 필요했는지는 `git diff --stat HEAD@{1}` 으로 한 번에 보입니다.

#### 동작 검증

```bash
sudo systemctl status myphotos-api myphotos-worker myphotos-ml-worker
curl -s http://localhost:8888/healthz | python3 -m json.tool
sudo journalctl -u myphotos-api -n 20 --no-pager
```

`/healthz` 응답의 `version` 이 새 값으로 바뀌고, status가 셋 다
`active (running)` 이면 성공.

#### 브라우저 캐시

프론트(`index.html`, `admin.html`) 변경된 commit이 섞여있는데도 UI가
그대로면 브라우저 캐시 때문입니다 — 강제 새로고침 (`Ctrl+Shift+R`,
모바일은 주소창 당겨서 새로고침).

#### 외부 바이너리 업데이트 (드물게)

`exiftool`/`ffmpeg` 새 버전을 받으려면:
```bash
./scripts/install-vendor-linux-x64.sh
sudo systemctl restart myphotos-worker
```
ML 모델은 한 번 받으면 거의 갱신 안 되지만 새 모델 commit이 있으면:
```bash
./scripts/install-ml-models.sh
sudo systemctl restart myphotos-ml-worker
```

#### 롤백

뭐가 잘못된 것 같으면 이전 commit으로 되돌리기:
```bash
git log --oneline -10                   # 직전 commit 해시 확인
git reset --hard <hash>
uv pip install --python .venv/bin/python -e .
.venv/bin/python -m alembic downgrade -1   # 스키마도 되돌릴 때만
sudo systemctl restart myphotos-api myphotos-worker myphotos-ml-worker
```

⚠️ `alembic downgrade` 는 데이터 손실 가능성이 있는 마이그레이션이면
실패할 수 있습니다. 그땐 백업(`scripts/backup-db.sh` 로 미리 떠둔
파일)을 복원하는 게 안전합니다.

#### 정기 백업 (cron / DSM 작업 스케줄러)

DSM **제어판 → 작업 스케줄러 → 사용자 정의 스크립트** 에 매일:
```bash
/var/services/homes/<user>/myphotos/scripts/backup-db.sh
```
`data/backups/` 에 최근 14개 자동 보관됩니다.

### 사진 폴더에서 직접 파일을 옮기거나 지우면 어떻게 되나

워커는 정해진 주기(기본 매일)와 관리 → 사진 폴더의 **스캔** 버튼으로
풀스캔을 돌립니다. 풀스캔이 같은 root를 처음부터 끝까지 훑으면서:

| 변경 종류 | 처리 |
| --- | --- |
| **새 파일 추가** | 행 신규 추가 + 인덱싱 잡 (해시/EXIF/썸네일) |
| **내용 변경** (size·mtime 바뀜) | `content_signature` 불일치 감지 → EXIF/썸네일 재처리 |
| **파일 삭제** | 같은 경로가 walk 결과에 없음 → `status='missing'` 으로 자동 마킹. 갤러리/지도/검색/중복에서 즉시 사라짐. DB 행(평점·코멘트·태그·공유링크 등)은 보존 |
| **파일 이름 변경** | 옛 경로는 missing, 새 경로는 신규 추가. 같은 sha256이면 라이트박스의 ⚏ 중복 칩에서 두 행이 같은 파일임이 보임 |
| **폴더 이름 변경 / 이동** | 같은 패턴 — 옛 위치 전부 missing, 새 위치 전부 신규 |
| **권한 갑자기 막힘** | scandir 실패 로그 기록, 행은 그대로 (false missing 방지). 다음 정상 스캔에서 일관성 회복 |
| **사라졌던 파일 다시 나타남** | 같은 경로에 동일 파일 발견 시 자동 복구 (`missing` → `active`) |

이 reconciliation은 **풀스캔(`limit` 없이) 에서만** 동작합니다. 200장
샘플 스캔은 자기가 보지 못한 파일이 지워졌다고 판단하면 위험하니까요.

**실시간 감지(watchdog) — 선택적 활성화**

기본은 daily 풀스캔 + 수동 트리거. 변경을 즉시 반영하고 싶으면 별도
워처 서비스를 켤 수 있습니다. inotify로 root를 구독하고, 변경 이벤트가
30초 동안(설정 가능) 잠잠해지면 그 root에 `discover_root` 잡을 자동
enqueue합니다.

켜는 법:

```bash
# 1. config/local.toml 에 추가
[watcher]
enabled = true
# debounce_seconds = 30          # 기본값
# reconcile_roots_seconds = 60   # 기본값
```

```bash
# 2. systemd 유닛 설치 (install-systemd.sh가 *.service.in 다 잡음)
./scripts/install-systemd.sh
sudo systemctl enable myphotos-watcher
sudo systemctl start  myphotos-watcher
sudo systemctl status myphotos-watcher
sudo journalctl -u myphotos-watcher -f
```

inotify watch 한도 (10만+ 폴더면 필요):

```bash
echo "fs.inotify.max_user_watches=524288" | sudo tee -a /etc/sysctl.conf
echo "fs.inotify.max_user_instances=512"  | sudo tee -a /etc/sysctl.conf
sudo sysctl -p

# 확인
find /volume1/photo -type d | wc -l                  # 등록할 폴더 수
cat /proc/sys/fs/inotify/max_user_watches            # 한도
```

> ⚠️ **한계** — inotify는 호스트 OS 파일시스템 변경만 감지합니다.
> 외부에서 SMB로 접속해 변경하는 것은 DSM의 samba 데몬이 쓰는
> 것이므로 보통 잡힙니다. 외부 NAS의 NFS 마운트, S3FS 같은 가상
> 파일시스템은 못 잡습니다 — 그쪽은 daily 풀스캔이 백업입니다.

#### 동작 상태 확인 (watcher 진단)

```bash
# 1. systemd 단의 살아있음
sudo systemctl status myphotos-watcher
# Active: active (running) 이어야 함

# 2. 부팅 로그 — 구독한 root 수 / 도구 감지 / catch-up
sudo journalctl -u myphotos-watcher -n 50 --no-pager
# 정상: "watcher observer started"
#       "watcher: subscribed root id=1 (/volume1/photo)"
#       "watcher: catch-up touched 1 root(s)"

# 3. 실시간 로그 — 파일 추가/변경 시 이벤트 흐름 보기
sudo journalctl -u myphotos-watcher -f
# 사진 폴더에 파일 한 개 던지고 ~30초 후
# "watcher: enqueued discover_root for root id=N" 떠야 정상

# 4. API에서 한 줄 — 별도 SSH 없이 확인 가능
curl -s http://localhost:8888/healthz | python3 -m json.tool
# watcher 블록에:
#   alive_at      : 최근 heartbeat 시각 (~2초마다 갱신)
#   age_seconds   : 마지막 heartbeat이 몇 초 전인지
#   stale         : true → 죽었거나 멈춤 (15초 이상 무응답)
#   watched_root_ids : 구독 중인 root id 목록
#   pending_roots    : 현재 debounce 큐에 들어있는 root 수
```

자주 막히는 케이스:

| 증상 | 원인 / 해결 |
| --- | --- |
| `watcher disabled in config (watcher.enabled=false)` 후 종료 | `config/local.toml`에 `[watcher] enabled = true` 추가 후 재시작 |
| `Active: active (running)` 인데 `/healthz` `stale: true` | 프로세스는 살았지만 dispatcher가 멈춤 — `journalctl -u myphotos-watcher --since "10 min ago"` 로 traceback 확인 |
| `schedule failed ... No space left on device` | `fs.inotify.max_user_watches` 한도 초과. 위 sysctl 명령으로 늘리기 |
| `watched_root_ids: []` | DB에 enabled root 없음. 관리 → 사진 폴더에서 enable, 또는 root 추가 |
| 이벤트 발생해도 `enqueued discover_root` 안 뜸 | (1) ignore 패턴에 걸림 (.tmp, @eaDir 등), (2) 30초 debounce 대기 중, (3) 기존 discover_root 잡 inflight 중 |

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
| 사진 폴더 root가 **`접근 불가`** | Synology Photos가 만든 폴더는 보통 `d---------+` (ACL 전용)이라 systemd가 실행하는 `$USER` 계정으로는 못 읽습니다. `ls -la /volume1/photo`로 확인하고 `sudo chmod 777 /volume1/photo` (또는 위 9단계의 `synoacltool` ACL 추가). |
| 잡 큐에 잡이 계속 쌓이고 줄지 않음 | 워커가 죽었거나 이전 잘못된 잡들이 큐를 막고 있을 수 있음. `sudo systemctl status myphotos-worker`로 워커 살아있는지 확인 → 죽었으면 `sudo journalctl -u myphotos-worker -n 60`. 큐 비우려면 관리 → 색인 → 잡 큐 → "대기·실패 잡 비우기" 또는 CLI `curl -X POST http://localhost:8888/api/admin/jobs/purge -H "Content-Type: application/json" -d '{"include_running":true}'`. |
| 타임라인이 비거나 500 오류 | `alembic current`가 `(head)`인지 확인. 아니면 `alembic upgrade head` 후 재시작 |
| 색인이 너무 느림 | 관리 → 설정 → 워커 → `concurrency` 조정. HDD면 3~4가 더 빠를 수 있음 |
| 워커 좀비 (status에 두 개 떠 있음) | `ps -ef \| grep app.worker`로 확인 후 systemd 외부 프로세스 `kill` |
| ML 워커가 active되자마자 죽음 | `journalctl -u myphotos-ml-worker -n 30`에 `model missing` 있으면 `./scripts/install-ml-models.sh` 미실행. 받은 후 재시작 |
| ML 분류 잡 다수가 failed | 모델 출력 형식이 코드 기대와 다른 변종일 수 있음. 위 로그의 traceback과 함께 이슈 등록 |
| admin 비밀번호 잊음 | `.venv/bin/python -c "from app.auth import hash_password; print(hash_password('새비번'))"` → 출력 해시를 sqlite3로 `UPDATE users SET password_hash='<해시>' WHERE username='admin';` |

## 외부 DB (MariaDB) 사용 (선택)

기본은 `data/catalog.db` (SQLite, 단일 파일)이며 대부분의 가족 운영
환경에선 충분합니다. 기존 NAS의 MariaDB를 카탈로그로 같이 쓰고 싶다면
DSN을 설정해서 백엔드를 바꿀 수 있습니다.

### 0) DB와 사용자 준비 (MariaDB 측에서)

```sql
CREATE DATABASE myphotos
    CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
CREATE USER 'myphotos'@'%' IDENTIFIED BY '강한_비밀번호';
GRANT ALL PRIVILEGES ON myphotos.* TO 'myphotos'@'%';
FLUSH PRIVILEGES;
```

### 1) MariaDB 드라이버 설치

```bash
uv pip install --python .venv/bin/python -e ".[mariadb]"
```

순수 Python 드라이버(`PyMySQL`)라 `libmariadb-dev` 같은 시스템 패키지가
필요 없습니다.

### 2) DSN 설정

`config/local.toml`에 추가:

```toml
[database]
url = "mysql+pymysql://myphotos:강한_비밀번호@DB호스트:3306/myphotos?charset=utf8mb4"
```

### 3) 기존 카탈로그 이전 (양방향)

마이그레이션 도구는 **양방향 모두**를 지원합니다 — SQLite → MariaDB,
MariaDB → SQLite, 또는 같은 종류끼리. 앱은 반드시 멈춘 상태에서 실행하세요.

```bash
sudo systemctl stop myphotos-api myphotos-worker myphotos-ml-worker
```

**SQLite → MariaDB (가장 흔한 경우)**
```bash
.venv/bin/python scripts/migrate-db.py \
    sqlite:///data/catalog.db \
    "mysql+pymysql://myphotos:강한_비밀번호@DB호스트:3306/myphotos?charset=utf8mb4" \
    --drop
```

**MariaDB → SQLite (원상복귀)**
```bash
.venv/bin/python scripts/migrate-db.py \
    "mysql+pymysql://myphotos:강한_비밀번호@DB호스트:3306/myphotos?charset=utf8mb4" \
    sqlite:///data/catalog.db \
    --drop
```

> 이전 이름 `scripts/migrate-sqlite-to-mariadb.py` 도 호환을 위해 그대로
> 동작합니다 (내부에서 위 스크립트를 호출).

`--drop`은 대상의 모든 테이블을 비우고 다시 만들므로 첫 이전에만 사용합니다.
스크립트는 끝나는 시점에 source/target 행 수를 비교하여 일치하지 않으면
오류로 종료합니다. AUTO_INCREMENT 카운터도 자동으로 끝값+1로 리셋합니다.

마이그레이션 후엔 `config/local.toml` 의 `database.url` 을 새 백엔드에
맞춰 수정한 뒤 서비스를 다시 시작합니다.

```bash
sudo systemctl start myphotos-api myphotos-worker myphotos-ml-worker
```

새 설치라면 위 마이그레이션 단계는 생략하고 그냥 `alembic upgrade head`
하면 됩니다 (`database.url`이 설정되어 있으면 자동으로 MariaDB에 스키마
생성됩니다).

### 양쪽이 어떻게 동기화 되나?

**동기화되지 않습니다.** 어느 한 시점엔 한쪽만 "메인"입니다:

- `database.url` 비어있음 → SQLite 가 메인, MariaDB 는 (있어도) 무관
- `database.url` 설정 → MariaDB 가 메인, SQLite 파일은 그냥 옛 스냅샷

이중 쓰기/실시간 복제는 일관성·실패 처리·분산 락이 따라붙어 가정용
NAS에는 과합니다. 대신 백업과 마이그레이션 도구로 같은 효과를 냅니다:

| 시나리오 | 절차 |
| --- | --- |
| **정기 백업** | `scripts/backup-db.sh` 를 매일 cron. SQLite 모드면 자동으로 `.db` 스냅샷, MariaDB 모드면 `mysqldump`. |
| **양쪽에 같은 데이터 두기** | `--both`로 백업 1회 → 다른 쪽 DB에 복원 1회. 그 시점부터는 한쪽이 메인이고 다른 쪽은 콜드 스탠바이. |
| **메인 백엔드 전환** | 위 마이그레이션 스크립트 한 번 + `database.url` 변경 + 재시작. |
| **장애 복구** | 마지막 백업으로 새 인스턴스에 복원, `database.url` 그대로 두고 서비스 시작. |

> 멀티 마스터가 정말 필요해진다면(가족 NAS 규모에서 보통 불필요) MariaDB
> Galera 클러스터 등을 구성하고 잡 큐 패턴을 `SELECT ... FOR UPDATE
> SKIP LOCKED` 로 바꿔야 하는데, 그 변경은 의도적으로 미뤄놓은 상태입니다.

### 4) 백업 스크립트

```bash
# 자동 — local.toml의 URL 따라 알맞은 백업
./scripts/backup-db.sh             # 기본 SQLite
./scripts/backup-db.sh --mariadb   # mysqldump
./scripts/backup-db.sh --both      # 둘 다 (이중 보험)
```

결과는 `data/backups/catalog-YYYYMMDD-HHMMSS.{db,sql.gz}`. 최근 14개씩만
보관합니다. cron / DSM 작업 스케줄러로 매일 돌리면 됩니다.

### 어느 쪽을 골라야 하나

- **SQLite (기본)**: 파일 1개, 별도 서버 불필요, 가족 단위 부하면 충분.
  포팅성 최강 — 디렉토리만 옮기면 됨.
- **MariaDB**: 다른 서비스와 같은 DB 서버에 묶고 싶을 때, 정기 백업이
  이미 MariaDB 기준으로 잡혀있을 때, 수십만~수백만 장 + 다중 동시 쓰기가
  생길 때. 포팅 시 MariaDB 인스턴스를 같이 챙겨야 함.

워커의 잡 큐 패턴(`UPDATE ... WHERE id = (SELECT ... LIMIT 1)`) 은
양쪽 모두에서 동작하므로 코드 분기는 PRAGMA/pool 옵션 정도뿐입니다.

## Docker 배포 (대안)

NAS에 Python/uv/exiftool/ffmpeg를 직접 설치하지 않고 컨테이너로 굴리고
싶을 때. 단일 이미지로 API + 인덱싱 워커 + (선택) ML 워커 3개 컨테이너를
띄웁니다.

### 0) 사전 준비

- Docker 20.10+ / Docker Compose v2 (DSM 7.2+는 "Container Manager"
  패키지에 둘 다 포함)
- **Git** — DSM 패키지 센터에서 "Git Server" 설치 (코드 clone에 필요).
  검증은 `git --version`. 직접 설치 섹션의 "DSM에 Git 설치하기" 박스 참고
- 사진 폴더 경로(호스트 측) — 예: `/volume1/photo`
- runtime 데이터를 둘 호스트 경로 — 예: `/volume1/docker/myphotos/data`

#### DSM(시놀로지)에서 docker CLI가 안 잡힐 때

DSM의 Container Manager는 `docker` 바이너리를 기본 PATH에 노출하지
않습니다. SSH로 `docker --version`이 안 먹으면 다음 중 하나:

```bash
# A. 실제 경로로 한 번에 — 위치 먼저 찾기
find /var/packages -name docker -type f 2>/dev/null
# 보통: /var/packages/ContainerManager/target/usr/bin/docker

# B. PATH에 영구 등록 (한 번만)
echo 'export PATH="/var/packages/ContainerManager/target/usr/bin:$PATH"' >> ~/.profile
source ~/.profile
docker --version            # OK
```

그리고 DSM Container Manager는 보통 v1 바이너리(`docker-compose`,
하이픈)는 PATH에 잡혀있지만 v2 plugin(`docker compose`, 띄어쓰기)은
등록이 안 돼서 `'compose' is not a docker command` 에러가 납니다.
선택:

```bash
# A. v1 그대로 쓰기 (가장 빠름) — 이하 README의 모든
# `docker compose ...` 명령을 `docker-compose ...`로 치환하면 됩니다.
docker-compose --version

# B. v2 plugin 등록 (한 번만)
mkdir -p ~/.docker/cli-plugins
ln -sf /var/packages/ContainerManager/target/usr/libexec/docker/cli-plugins/docker-compose \
       ~/.docker/cli-plugins/docker-compose
docker compose version
```

> ⚠ 이후 명령들은 가독성 위해 `docker compose` (v2 스타일)로 쓰지만,
> v1을 쓰시면 모두 하이픈 버전으로 바꿔 읽으세요. 동작은 동일.

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

> ⚠ **사진 폴더 경로 등록 (가장 자주 막히는 부분)**: 관리 UI →
> 사진 폴더 → 새 폴더 추가의 "절대 경로"에는 **호스트 경로가 아닌
> 컨테이너 안 경로** 를 입력합니다.
>
> - 직접 설치: `/volume1/photo` (호스트 경로 그대로)
> - 도커: `/photos` (compose에서 `${PHOTO_ROOT}:/photos:ro`로 마운트되므로)
> - 폴더를 여러 개 마운트한 경우: `/photos2`, `/photos3` …

### Docker 트러블슈팅

| 증상 | 원인 / 해결 |
| --- | --- |
| 관리 UI에 root 추가했더니 상태가 **`접근 불가`** | 거의 항상 다음 둘 중 하나입니다. ① 경로가 컨테이너 안 경로가 아니라 호스트 경로(`/volume1/photo`)로 들어감 → `/photos`로 수정. ② Synology Photos가 만든 폴더 권한이 `d---------+`(ACL 전용)이라 컨테이너 UID로 못 읽음 → 호스트에서 `sudo chmod 777 /volume1/photo` 한 번. |
| 컨테이너에서 사진이 진짜 보이는지 빠르게 확인 | `docker compose exec api ls /photos \| head` — 파일이 보여야 정상. `Permission denied`면 위 ② 권한 문제. |
| `docker: 'compose' is not a docker command` | DSM Container Manager에 v2 plugin이 등록 안 된 상태. 위 "DSM에서 docker CLI가 안 잡힐 때" 섹션의 plugin 등록 또는 `docker-compose` (하이픈) 사용. |
| `Bind for 0.0.0.0:8888 failed: port is already allocated` | 이전에 띄운 MyPhotos 컨테이너가 같은 포트를 잡고 있는 경우가 대부분. `docker ps --format '{{.Names}}\t{{.Ports}}' \| grep 8888`으로 찾고, `docker ps -aq --filter 'name=myphotos' \| xargs -r docker rm -f` 또는 이전 폴더에서 `docker compose down`. 그 외 다른 서비스가 점유했다면 `.env`의 `API_PORT`를 9888 등으로 변경. |
| `git clone .` 실행 시 `destination path '.' already exists` | 폴더에 뭔가 남아있는 상태. 깨끗하게 다시 받기: `cd .. && rm -rf myphotos && mkdir myphotos && cd myphotos && git clone https://github.com/saintsc-ai/MyPhotos.git .` (DATA_DIR이 같은 폴더 안의 `data/`였다면 미리 옮겨두기) |
| 스캔/색인이 멈춰 보이고 잡 큐가 계속 쌓임 | 이전에 잘못된 경로·권한으로 등록된 잡들이 큐를 막고 있는 경우가 많습니다. 관리 → **색인** 탭 → **잡 큐** 섹션의 "대기·실패 잡 비우기" 또는 "실행 중 포함 전체 비우기" 버튼으로 정리한 뒤 다시 스캔. CLI로도 가능: `curl -X POST http://NAS:8888/api/admin/jobs/purge -H "Content-Type: application/json" -d '{"include_running":true}'` |
| Synology Photos가 같은 폴더에 쓰는 중인데 충돌이 걱정 | `read-only` 옵션을 켠 상태(권장)면 MyPhotos는 원본을 절대 수정하지 않습니다. ACL 권한만 풀어주면 됨. |
| UID/GID를 바꿨는데 반영 안 됨 | `APP_UID/GID`는 빌드 인자라서 `docker compose up -d --build` 로 이미지를 다시 빌드해야 적용됩니다 (GHCR 이미지를 쓸 땐 변경 효과가 제한적이라 호스트 폴더에 `chmod` 쪽이 더 단순). |
| 새 컨테이너가 빈 DB로 시작 (이전 데이터 안 보임) | 이전 컨테이너의 `DATA_DIR`을 새 `.env`가 다른 위치로 가리키고 있는 경우. `docker inspect 이전컨테이너 --format '{{range .Mounts}}{{.Source}} → {{.Destination}}\n{{end}}'`로 옛 위치 확인 → 새 `.env`의 `DATA_DIR`을 그쪽으로 바꾸거나 데이터를 새 위치로 옮기기. |

### 컨테이너 안 vs 호스트 명령 — 어디서 무엇을 실행?

| 명령 종류 | 실행 위치 | 예 |
| --- | --- | --- |
| `docker compose ...` | **호스트** | `docker compose up -d`, `docker compose logs -f worker` |
| 호스트 파일 권한·경로 조작 | **호스트** | `chmod 777 /volume1/photo` (컨테이너 안에선 `:ro`라 의미 없음) |
| HTTP 호출 (`curl /api/...`) | **어디서든** (NAS에 닿기만 하면) | `curl http://NAS:8888/healthz` |
| 컨테이너 내부 확인 / 디버깅 | **컨테이너 안 한 줄** | `docker compose exec api ls /photos`, `docker compose exec api bash` |
| `alembic upgrade head` | **자동** (entrypoint가 시작 시 실행) | 수동 호출 불필요 |

운영 명령은 거의 다 호스트에서. 컨테이너 안 쉘은 디버깅용으로만.

### `git pull` vs `docker compose pull` — 언제 무엇이 필요?

| 변경된 게 | NAS에서 필요한 명령 |
| --- | --- |
| 앱 코드만 (대부분의 commit) | `docker compose pull && docker compose up -d` — `git pull` **불필요** |
| `docker-compose.yml`·`.env.example`·`Dockerfile` | `git pull && docker compose pull && docker compose up -d` |
| 헷갈리면 안전하게 셋 다 | 위 두 줄 다 — 변경 없는 단계는 no-op |

확실히 알고 싶으면:

```bash
git fetch && git log HEAD..origin/main --name-only --pretty=format:'%h' \
  | grep -E '^(docker-compose|\.env|Dockerfile|\.dockerignore)' \
  && echo "→ git pull 필요" || echo "→ docker compose pull 만으로 충분"
```

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
| **Git** | Install from DSM Package Center ("Git Server"). Required for the `git clone` step; without it the bootstrap fails with `command not found` |
| **Perl** | Install from DSM Package Center ("Perl"). The bundled exiftool is a Perl script; without Perl, RAW/HEIC EXIF extraction fails |
| Photo root folder | e.g. `/volume1/photo`, readable by the user |
| Port 8888 free | otherwise see [post-install](#post-install) |

> **Installing Git on DSM**: Package Center → All Packages → search
> "Git Server" → install. It's an official Synology package — despite
> the "Server" name, the git CLI (`git` command) ships with it. Verify
> over SSH with `git --version`. If it says `command not found`, the
> binary isn't on PATH — the real path is
> `/var/packages/Git/target/bin/git`, so add
> `export PATH="/var/packages/Git/target/bin:$PATH"` to `~/.profile`.

<!-- -->

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

> ⚠ **Folder permissions** — if the root row flips to `접근 불가`
> (no access), the folder is owned by a user the systemd `$USER`
> account can't read. Folders created by Synology Photos default to
> `d---------+` (ACL-only) and stay invisible even to the owner over
> POSIX permissions. One-shot fix on the host:
>
> ```bash
> ls -la /volume1/photo            # check for d---------+
> sudo chmod 777 /volume1/photo
> ```
>
> Synology Photos is unaffected, and MyPhotos won't modify originals
> when read-only stays checked. For a tighter grant, add an ACL entry
> instead of opening to everyone:
>
> ```bash
> sudo synoacltool -add /volume1/photo "user:$USER:allow:r-x---a-R-c--:fd--"
> ```

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
| Root row shows **`접근 불가`** (no access) | Synology Photos folders are usually `d---------+` (ACL-only) and unreadable by the systemd `$USER`. `ls -la /volume1/photo` to confirm, then `sudo chmod 777 /volume1/photo` (or the `synoacltool` ACL entry from step 9). |
| Queue keeps growing, jobs aren't progressing | Worker may be dead, or stale jobs from a bad earlier run are blocking. Check `sudo systemctl status myphotos-worker`; if it's running, purge the queue via Admin → 색인 → 잡 큐 → "대기·실패 잡 비우기", or `curl -X POST http://localhost:8888/api/admin/jobs/purge -H "Content-Type: application/json" -d '{"include_running":true}'`. |
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
- **Git** — install "Git Server" from the DSM Package Center (needed
  for `git clone`). Verify with `git --version`. See the
  "Installing Git on DSM" box in the direct-install section above
  for the full walkthrough.
- Host path to the photo library — e.g. `/volume1/photo`
- Host path for runtime data — e.g. `/volume1/docker/myphotos/data`

#### When the docker CLI isn't on PATH (DSM/Synology)

DSM's Container Manager doesn't expose `docker` on the default PATH.
If `docker --version` over SSH errors out:

```bash
# Find the binary
find /var/packages -name docker -type f 2>/dev/null
# Usually: /var/packages/ContainerManager/target/usr/bin/docker

# Add to PATH permanently
echo 'export PATH="/var/packages/ContainerManager/target/usr/bin:$PATH"' >> ~/.profile
source ~/.profile
```

And DSM Container Manager registers the v1 binary (`docker-compose`,
hyphenated) on PATH but doesn't wire up the v2 plugin
(`docker compose`, space). Pick one:

```bash
# A. Use v1 as-is — substitute every `docker compose` in this README
#    with `docker-compose`. Functionally identical.
docker-compose --version

# B. Register the v2 plugin once
mkdir -p ~/.docker/cli-plugins
ln -sf /var/packages/ContainerManager/target/usr/libexec/docker/cli-plugins/docker-compose \
       ~/.docker/cli-plugins/docker-compose
docker compose version
```

> ⚠ The rest of the README uses `docker compose` (v2 spacing). If
> you went with v1, mentally hyphenate every such command.

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

> ⚠ **Adding a root (the #1 thing that trips people up)**: Admin →
> 사진 폴더 → 새 폴더 추가 → the "절대 경로" field expects the
> **in-container** path, not the host path.
>
> - Direct install: `/volume1/photo` (host path as-is)
> - Docker: `/photos` (because compose mounts `${PHOTO_ROOT}:/photos:ro`)
> - Extra mounts: `/photos2`, `/photos3`, …

### Docker troubleshooting

| Symptom | Cause / fix |
| --- | --- |
| Root row shows **`접근 불가` (no access)** | Almost always one of: ① the path was entered as a host path (`/volume1/photo`) instead of the in-container path → edit to `/photos`. ② Synology Photos created the folder with `d---------+` (ACL-only) so the container UID can't read it → `sudo chmod 777 /volume1/photo` on the host once. |
| Quick sanity check from outside the UI | `docker compose exec api ls /photos \| head` — files visible = OK. `Permission denied` means the ACL issue above. |
| `docker: 'compose' is not a docker command` | Container Manager didn't register the v2 plugin. See the "When the docker CLI isn't on PATH" subsection above for plugin registration, or just use `docker-compose` (hyphenated). |
| `Bind for 0.0.0.0:8888 failed: port is already allocated` | Usually a leftover MyPhotos container is still holding the port. `docker ps --format '{{.Names}}\t{{.Ports}}' \| grep 8888` to find it, then `docker ps -aq --filter 'name=myphotos' \| xargs -r docker rm -f`, or `docker compose down` from the old folder. If another service owns the port, change `API_PORT` in `.env` (e.g. to 9888). |
| `git clone .` says `destination path '.' already exists` | Folder isn't empty. Cleanest restart: `cd .. && rm -rf myphotos && mkdir myphotos && cd myphotos && git clone https://github.com/saintsc-ai/MyPhotos.git .` (move `data/` aside first if it lives inside that folder). |
| Scans seem stuck, queue keeps growing | Usually a backlog of jobs from an earlier misconfigured run is blocking the queue. Admin → **색인** tab → **잡 큐** section → "대기·실패 잡 비우기" (or "실행 중 포함 전체 비우기" if a worker is wedged). CLI equivalent: `curl -X POST http://NAS:8888/api/admin/jobs/purge -H "Content-Type: application/json" -d '{"include_running":true}'` |
| Synology Photos is writing to the same folder concurrently | With `read-only` checked (default), MyPhotos never modifies originals. Only the ACL/permission needs fixing. |
| Changed `APP_UID/GID` but it didn't take effect | These are build args, so `docker compose up -d --build` is required (or use the GHCR image and rely on host-side `chmod` instead — simpler). |
| New container starts with an empty DB (old data missing) | The new `.env`'s `DATA_DIR` points somewhere different from the previous container. Find the old path with `docker inspect 이전_컨테이너 --format '{{range .Mounts}}{{.Source}} → {{.Destination}}\n{{end}}'`, then either point `DATA_DIR` at it or move the old data into the new location. |

### Inside the container vs on the host — where do I run things?

| Command type | Where | Example |
| --- | --- | --- |
| `docker compose ...` | **Host** | `docker compose up -d`, `docker compose logs -f worker` |
| Host file permissions / paths | **Host** | `chmod 777 /volume1/photo` (no-op inside the read-only mount) |
| HTTP calls (`curl /api/...`) | **Anywhere with network to the NAS** | `curl http://NAS:8888/healthz` |
| Container-internal inspection / debug | **One-shot from host** | `docker compose exec api ls /photos`, `docker compose exec api bash` |
| `alembic upgrade head` | **Automatic** (entrypoint runs it on start) | — |

Day-to-day ops happen on the host. Container shells are for debugging only.

### `git pull` vs `docker compose pull` — when do I need each?

| What changed | What to run on the NAS |
| --- | --- |
| App code only (most commits) | `docker compose pull && docker compose up -d` — `git pull` **not needed** |
| `docker-compose.yml` / `.env.example` / `Dockerfile` | `git pull && docker compose pull && docker compose up -d` |
| Not sure? Run both safely | Both lines — unchanged steps no-op |

To check without guessing:

```bash
git fetch && git log HEAD..origin/main --name-only --pretty=format:'%h' \
  | grep -E '^(docker-compose|\.env|Dockerfile|\.dockerignore)' \
  && echo "→ git pull needed" || echo "→ docker compose pull alone is enough"
```

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

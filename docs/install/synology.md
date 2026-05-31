# Synology NAS 설치 가이드

> 한국어 / [English](#english)

> [← README로 돌아가기](../../README.md)

DSM 7.x + 시스템에 직접 설치(systemd) 시나리오. 검증 호스트: DS3622xs+ (시놀로지, x86_64). 도커로 굴리고 싶으면 [Docker 가이드](docker.md)를 참고하세요.

> 아래 명령들은 모두 `~`(현재 사용자의 홈)와 `$USER`(현재 사용자명)를
> 사용하므로, 어떤 DSM 계정으로 로그인했든 그대로 복사·실행하면 됩니다.
> DSM의 사용자 홈은 보통 `/var/services/homes/$USER`인데 셸의 `~`가
> 이를 자동으로 가리킵니다.
>
> 설치 폴더 이름(여기서는 `myphotos`)도 원하는 이름으로 바꾸셔도 됩니다 —
> 이하 명령에서 `~/myphotos` 부분만 그에 맞춰 바꾸세요.

## 사전 준비

| 항목 | 비고 |
| --- | --- |
| DSM 사용자 계정 | 어떤 ID든 OK. `sudo` 권한 필요 (systemd 유닛 설치 시) |
| SSH 접근 | DSM 제어판 → 터미널 및 SNMP → SSH 활성 |
| 인터넷 | uv / 의존성 / vendor 바이너리 다운로드용 |
| **Git** | DSM 패키지 센터에서 "Git Server" 설치 (코드 clone에 필요). 미설치면 `git clone` 단계가 `command not found`로 실패 |
| **Perl** | DSM 패키지 센터에서 "Perl" 설치 (exiftool이 Perl 스크립트). 미설치면 RAW/HEIC EXIF 추출 실패 |
| 사진 root 폴더 | 예: `/volume1/photo`. 사용자에게 읽기 권한 |
| 8888 포트 | 다른 서비스가 안 쓰면 그대로. 점유 시 README의 [설치 후 운영](../../README.md#설치-후-운영) 참고 |

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

## 0) uv 설치 (1회만)

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

## 1) 코드 받기

```bash
cd ~
git clone https://github.com/saintsc-ai/MyPhotos.git myphotos
cd ~/myphotos
```

> 폴더 이름을 다르게 쓰고 싶다면 (예: `photo-server`) clone 끝의 인자를
> 바꾸세요: `git clone <URL> photo-server`. 이후 `~/photo-server`로 cd.

## 2) Python venv + 라이브러리 설치

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

## 3) exiftool / ffmpeg 설치 (RAW / HEIC / 동영상 썸네일용)

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

## 4) (선택) HEIC 직접 열기 활성화

iPhone HEIC를 Pillow로 직접 열어 더 빠르게 처리하고 싶을 때:
```bash
uv pip install --python .venv/bin/python -e ".[heic]"
```

설치 실패 시 (DSM glibc/wheel 호환 문제)는 그냥 넘겨도 됩니다 — exiftool이
HEIC 메타데이터/썸네일을 대신 처리합니다.

## 5) DB 스키마 생성

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

## 6) (선택) 호스트별 설정

대부분의 값은 설치 후 관리 UI에서 변경 가능합니다 (관리 → 설정 탭).
지금 손댈 게 거의 없습니다만, 미리 바꾸고 싶다면:

```bash
[ -f config/local.toml ] || cp config/local.example.toml config/local.toml
# 편집기로 열어 수정 — 예시: 워커 동시성, 앱 이름, 시간대 등
```

`secret_key`는 첫 부팅 시 `data/session.secret`에 자동 생성됩니다.

## 7) (선택) ML 자동 분류 모델 다운로드

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

## 8) systemd 서비스 등록

스크립트가 현재 사용자(`$USER`)와 설치 경로(`$PWD`)를 자동으로 채워서
세 unit 파일을 `/etc/systemd/system/`에 설치합니다:
- `myphotos-api.service` — FastAPI (uvicorn) 8888 포트
- `myphotos-worker.service` — 스캐너 + 인덱싱 워커
- `myphotos-ml-worker.service` — ML 워커 (YOLO / CLIP / 얼굴). 7단계를
  스킵했으면 그냥 안 켜고 둬도 됩니다 — 잡이 안 들어오니 idle 상태로 머묾.

```bash
./scripts/install-systemd.sh
sudo systemctl enable myphotos-api myphotos-worker myphotos-ml-worker
```

```bash
sudo systemctl start  myphotos-api myphotos-worker myphotos-ml-worker
```

> DSM의 옛 systemd 빌드는 `--now` 옵션을 지원 안 해서 `enable`과 `start`를
> 두 줄로 분리했습니다. ML 워커는 `Nice=15` / `IOSchedulingPriority=7`로
> 낮은 우선순위라 인덱싱 진행 중에도 공존합니다.

검증 — 셋 다 `Active: active (running)` 이어야 OK:

```bash
sudo systemctl status myphotos-api       | head -3
```

```bash
sudo systemctl status myphotos-worker    | head -3
```

```bash
sudo systemctl status myphotos-ml-worker | head -3
```

## 9) 첫 로그인 & 사진 폴더 등록

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

> ⚠ **사진 폴더 권한 (필수)** — root 추가 직후 상태가 `접근 불가`로
> 뜨거나, 나중에 회전/삭제 작업이 `Permission denied`로 실패한다면 폴더
> 권한 문제입니다. Synology Photos가 만든 `/volume1/photo`는 보통
> `d---------+` (ACL 전용)이라 systemd가 실행하는 `$USER` 계정으로는
> 읽기조차 안 됩니다. **읽기만** 되면 색인은 동작하지만, **쓰기까지**
> 풀려야 휴지통 이동(삭제)·EXIF 회전이 가능합니다.
>
> **사용 케이스별 권한**
>
> | 하고 싶은 것 | 필요한 권한 | 명령 |
> |---|---|---|
> | 색인·썸네일만 (원본 절대 안 건드림) | top 디렉토리 읽기 | `sudo chmod 755 /volume1/photo` |
> | + 회전·삭제·휴지통 이동 | 전 트리 읽기 + 디렉토리 쓰기 | `sudo ./scripts/fix-photo-perms.sh /volume1/photo` |
>
> 두 번째는 다음 두 줄과 동등:
>
> ```bash
> sudo chown -R $USER:users /volume1/photo
> ```
> ```bash
> sudo chmod -R u+rwX,g+rX,o+rX /volume1/photo
> ```
>
> `u+rwX` = 소유자에게 read/write + (대문자 `X`는) **디렉토리에만** 진입
> 권한 추가, 일반 파일엔 실행권 안 줌. 회전/삭제 시 exiftool이 같은
> 폴더에 `<file>_exiftool_tmp` 임시 파일을 만들기 때문에 **디렉토리 쓰기
> 권한**이 핵심입니다.
>
> 변경 후 확인:
>
> ```bash
> ls -ld /volume1/photo /volume1/photo/*/ | head    # 디렉토리에 'w' 있나
> sudo journalctl -u myphotos-api -f                # 회전/삭제 다시 시도하면서 로그 확인
> ```
>
> **Synology Photos와의 공존** — 권한을 풀어도 Synology Photos는 영향
> 없이 계속 동작합니다. 관리 → 사진 폴더에서 root를 **readonly**로 켜두면
> MyPhotos도 원본을 절대 수정하지 않으므로 안전합니다.
>
> 더 정교하게 가려면 Synology ACL로 `$USER`만 read 추가 (이 경우엔
> MyPhotos에서 회전·삭제는 안 되고 색인만 됩니다):
>
> ```bash
> sudo synoacltool -add /volume1/photo "user:$USER:allow:r-x---a-R-c--:fd--"
> ```

## 10) (선택) ML 자동 분류 시작

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

## 11) (선택) 가족 사용자 추가

관리 → **사용자** 탭 → **새 사용자 추가**:
- 사용자명: `mom`, `dad` 등
- 비밀번호: 임의 설정
- 관리자 권한: 보통 X (보기·공유·태그·코멘트 가능, 삭제는 불가)

## 12) (선택) 외부 노출

기본은 LAN 전체 (`0.0.0.0:8888`). WAN에서 쓰려면:
- DSM 제어판의 **역방향 프록시** 룰로 HTTPS 도메인 → `localhost:8888`
- 또는 [Tailscale](https://tailscale.com) 등 VPN 메시

자체 세션 쿠키 인증이라 외부 LB가 그대로 통과해도 무방.

---

설치 후 운영(코드 업데이트, watcher, 백업, 트러블슈팅, 외부 DB, 호스트 이전 등)은 README의 [설치 후 운영](../../README.md#설치-후-운영) 섹션을 참고하세요.

---

# English

## Synology NAS install guide

> [← Back to README](../../README.md)

DSM 7.x + direct host install via systemd. Reference host: DS3622xs+
(Xpenology, x86_64). Want containers instead? See the
[Docker guide](docker.md).

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
| Port 8888 free | otherwise see the README's [post-install](../../README.md#post-install) section |

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

`./scripts/install-systemd.sh` fills `$USER` + `$PWD` into the templates.

```bash
./scripts/install-systemd.sh
sudo systemctl enable myphotos-api myphotos-worker myphotos-ml-worker
```

```bash
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

---

Post-install ops (code update, watcher, backup, troubleshooting, external
DB, host porting) are in the README's
[Post-install](../../README.md#post-install) section.

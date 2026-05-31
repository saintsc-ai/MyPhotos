# Linux 설치 가이드 (일반)

> 한국어 / [English](#english)

> [← README로 돌아가기](../../README.md)

Synology가 아닌 일반 Linux 호스트에 직접 설치 (Debian/Ubuntu/Fedora/Arch
등). 컨테이너로 쓰고 싶으면 [Docker 가이드](docker.md), Synology DSM이면
[Synology 가이드](synology.md)를 참고하세요.

검증 환경: Debian 12 / Ubuntu 22.04+ / Fedora 39+ / Arch (rolling).
systemd 기반이면 어떤 배포판이든 동작합니다.

## 사전 준비

| 항목 | 비고 |
| --- | --- |
| Linux 호스트 + systemd | 어떤 배포판이든 OK |
| sudo 권한 | systemd 유닛 설치 시 필요 |
| 인터넷 | uv / 의존성 다운로드 |
| **Git** | `git --version` 검증 |
| **Perl** | exiftool이 Perl 스크립트 (대부분 배포판 기본 설치됨; `which perl`로 확인) |
| 사진 폴더 | 예: `/srv/photos` 또는 `~/Pictures`. 실행 사용자에게 읽기 권한 |
| 8888 포트 | 다른 서비스가 안 쓰면 그대로 |

### 시스템 패키지 (배포판별)

```bash
# Debian / Ubuntu
sudo apt update && sudo apt install -y git perl curl ca-certificates

# Fedora / RHEL
sudo dnf install -y git perl curl ca-certificates

# Arch
sudo pacman -S --needed git perl curl ca-certificates
```

> `exiftool` / `ffmpeg`은 아래 vendor 스크립트가 받으므로 시스템 패키지로
> 따로 설치할 필요는 없습니다. 시스템 PATH에 이미 있으면 그쪽이 우선 사용됩니다.

## 0) uv 설치 (1회만)

[uv](https://docs.astral.sh/uv/)는 Python 버전 + venv를 한 번에 관리합니다.

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
source ~/.bashrc          # 또는 ~/.zshrc / ~/.profile
uv python install 3.11.9  # 사용자 영역에 Python 3.11.9 설치
```

검증:
```bash
uv --version              # → uv 0.x.y
which uv                  # ~/.local/bin/uv
```

## 1) 코드 받기

```bash
cd ~
git clone https://github.com/saintsc-ai/MyPhotos.git myphotos
cd ~/myphotos
```

## 2) Python venv + 라이브러리 설치

```bash
./scripts/bootstrap.sh
```

스크립트가 자동으로:
- `.venv/`에 Python 3.11 가상환경 생성
- `pyproject.toml` 의존성 설치 (`fastapi`, `sqlalchemy`, `bcrypt`, `pillow` 등)

검증:
```bash
.venv/bin/python --version    # → Python 3.11.x
```

## 3) exiftool / ffmpeg 설치

x86_64 Linux는 동봉 vendor 스크립트:

```bash
./scripts/install-vendor-linux-x64.sh
```

`vendor/linux-x64/`에 두 바이너리가 들어갑니다. ARM이나 시스템 패키지를 쓰고
싶으면:

```bash
# Debian / Ubuntu
sudo apt install -y exiftool ffmpeg libimage-exiftool-perl

# Fedora
sudo dnf install -y perl-Image-ExifTool ffmpeg

# Arch
sudo pacman -S perl-image-exiftool ffmpeg
```

시스템 패키지가 잡혀 있으면 `app/external.py`가 자동 감지합니다 (vendor →
설정 → PATH 순).

검증:
```bash
exiftool -ver                          # 또는 ./vendor/linux-x64/exiftool -ver
ffmpeg -version | head -1
```

## 4) (선택) HEIC 직접 열기 활성화

iPhone HEIC를 Pillow로 직접 열어 더 빠르게 처리하고 싶을 때:
```bash
uv pip install --python .venv/bin/python -e ".[heic]"
```

libheif 시스템 패키지가 필요할 수 있습니다:
```bash
# Debian/Ubuntu
sudo apt install -y libheif1 libheif-dev
```

설치 실패 시는 그냥 넘겨도 됩니다 — exiftool이 HEIC 메타데이터/썸네일을
대신 처리합니다.

## 5) DB 스키마 생성

```bash
.venv/bin/python -m alembic upgrade head
```

`data/catalog.db` (SQLite)가 생성됩니다.

검증:
```bash
.venv/bin/python -m alembic current
# 출력 끝줄에 (head)가 있어야 OK
```

## 6) (선택) 호스트별 설정

```bash
[ -f config/local.toml ] || cp config/local.example.toml config/local.toml
# 편집기로 열어 수정
```

`secret_key`는 첫 부팅 시 `data/session.secret`에 자동 생성됩니다.

## 7) (선택) ML 자동 분류 모델 다운로드

```bash
./scripts/install-ml-models.sh
```

ONNX 가중치 6개(~140MB)가 `data/models/`에 받아집니다. 받지 않으면 자동
분류만 비활성됩니다 (인덱싱/EXIF/썸네일/검색은 정상).

## 8) systemd 서비스 등록

```bash
./scripts/install-systemd.sh
sudo systemctl enable --now myphotos-api myphotos-worker myphotos-ml-worker
```

> 최신 systemd는 `--now`로 enable+start 한 줄. DSM은 옛 systemd라 두 줄로
> 분리해야 합니다. 시스템에서 `systemctl enable --now ...` 가 동작 안 하면:
> ```bash
> sudo systemctl enable myphotos-api myphotos-worker myphotos-ml-worker
> sudo systemctl start  myphotos-api myphotos-worker myphotos-ml-worker
> ```

검증:
```bash
systemctl status myphotos-api myphotos-worker myphotos-ml-worker | head -30
```

## 9) 첫 로그인 & 사진 폴더 등록

1. 브라우저에서 `http://<HOST-IP>:8888` 접속
2. **admin / admin** 로그인 → 비밀번호 변경
3. 우상단 **관리** → **사진 폴더** 탭 → **새 폴더 추가**:
   - **라벨**: `family` (영숫자/`_`/`-`)
   - **절대 경로**: 실제 사진 폴더 (예: `/srv/photos`)
   - **읽기 전용**: 체크 권장 — 원본 절대 안 건드림
4. **시험** 버튼으로 200장 샘플 → **색인** 탭에서 진행 확인
5. 정상이면 **사진 폴더** 탭으로 돌아가 limit 비우고 **스캔** → 풀스캔

### 폴더 권한 (필수)

systemd가 실행하는 사용자(`$USER`)에게 사진 폴더 읽기 권한이 있어야 합니다.
NAS의 다른 앱이 만든 폴더(예: 클라우드 동기화)는 보통 권한이 막혀 있습니다.

| 하고 싶은 것 | 필요한 권한 | 명령 |
|---|---|---|
| 색인·썸네일만 (원본 안 건드림) | 최상위 디렉토리 읽기 | `sudo chmod 755 /srv/photos` |
| + 회전·삭제·휴지통 이동 | 전 트리 읽기 + 디렉토리 쓰기 | `sudo chown -R $USER:$USER /srv/photos && sudo chmod -R u+rwX,g+rX,o+rX /srv/photos` |

`u+rwX`에서 대문자 `X`는 **디렉토리에만** 진입 권한 추가. 회전/삭제 시
exiftool이 같은 폴더에 `<file>_exiftool_tmp` 임시 파일을 만들기 때문에
**디렉토리 쓰기 권한**이 핵심입니다.

확인:
```bash
ls -ld /srv/photos /srv/photos/*/ | head    # 디렉토리에 'w' 있나
sudo journalctl -u myphotos-api -f          # 회전/삭제 재시도하면서 로그 확인
```

## 10) (선택) ML 자동 분류 시작

7단계에서 모델을 받았다면 관리 → **ML 자동 분류** 카드에서:
- 단계 체크박스 (`objects` / `embedding` / `faces`) — `objects`부터 limit
  200으로 smoke-test → 정상이면 셋 다 풀스케일
- `force_reclassify`는 보통 OFF
- 진행은 같은 카드의 stats 또는 `journalctl -u myphotos-ml-worker -f`

10만 장 기준 CPU로 반나절~하루.

## 11) (선택) 가족 사용자 추가

관리 → **사용자** → **새 사용자 추가**:
- 사용자명 / 비밀번호
- 관리자 권한은 보통 X (보기·공유·태그·코멘트 가능, 삭제 / EXIF 수정은 불가)

## 12) (선택) 외부 노출

기본은 `0.0.0.0:8888` LAN 전체 노출. WAN에서 쓰려면:
- nginx / Caddy / Traefik 등 리버스 프록시로 HTTPS 도메인 → `localhost:8888`
- 또는 [Tailscale](https://tailscale.com) / WireGuard 등 VPN

자체 세션 쿠키 인증이라 외부 LB가 그대로 통과해도 무방.

---

설치 후 운영(코드 업데이트, watcher, 백업, 트러블슈팅, 외부 DB, 호스트 이전 등)은 README의 [설치 후 운영](../../README.md#설치-후-운영) 섹션을 참고하세요.

---

# English

## Linux install guide (generic)

> [← Back to README](../../README.md)

Direct install on a non-Synology Linux host (Debian / Ubuntu / Fedora /
Arch, …). Containers? See the [Docker guide](docker.md). Synology DSM?
See the [Synology guide](synology.md).

Tested on: Debian 12 / Ubuntu 22.04+ / Fedora 39+ / Arch (rolling).
Any systemd-based distro should work.

### Prerequisites

| Item | Notes |
| --- | --- |
| Linux host with systemd | Any distro |
| sudo | needed for systemd unit install |
| Internet | for uv / dependency downloads |
| **Git** | verify with `git --version` |
| **Perl** | exiftool is a Perl script (usually preinstalled; check with `which perl`) |
| Photo folder | e.g. `/srv/photos` or `~/Pictures`, readable by the service user |
| Port 8888 free | otherwise pick another via `config/local.toml` |

### System packages (per distro)

```bash
# Debian / Ubuntu
sudo apt update && sudo apt install -y git perl curl ca-certificates

# Fedora / RHEL
sudo dnf install -y git perl curl ca-certificates

# Arch
sudo pacman -S --needed git perl curl ca-certificates
```

> `exiftool` / `ffmpeg` are handled by the vendor script below, so no
> need to install them via the system package manager. If they happen
> to be on PATH already, those take precedence.

### 0) Install uv (one time)

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
source ~/.bashrc          # or ~/.zshrc / ~/.profile
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

### 3) exiftool / ffmpeg

x86_64 Linux uses the bundled vendor script:

```bash
./scripts/install-vendor-linux-x64.sh
```

For ARM or if you'd rather use system packages:

```bash
# Debian / Ubuntu
sudo apt install -y exiftool ffmpeg libimage-exiftool-perl

# Fedora
sudo dnf install -y perl-Image-ExifTool ffmpeg

# Arch
sudo pacman -S perl-image-exiftool ffmpeg
```

`app/external.py` auto-detects (vendor → config → PATH).

### 4) (optional) Native HEIC reader

```bash
uv pip install --python .venv/bin/python -e ".[heic]"
```

May need libheif system packages:
```bash
sudo apt install -y libheif1 libheif-dev   # Debian/Ubuntu
```

Skip silently on failure — exiftool falls back automatically.

### 5) Create / upgrade the DB schema

```bash
.venv/bin/python -m alembic upgrade head
```

### 6) (optional) Host overrides

```bash
[ -f config/local.toml ] || cp config/local.example.toml config/local.toml
```

### 7) (optional) Download ML classification models

```bash
./scripts/install-ml-models.sh
```

### 8) Install systemd units

```bash
./scripts/install-systemd.sh
sudo systemctl enable --now myphotos-api myphotos-worker myphotos-ml-worker
```

(If your distro's systemd doesn't accept `--now`, split into
`enable` + `start`.)

### 9) First login + photo root

1. Open `http://<HOST-IP>:8888`
2. Sign in with **admin / admin**, change the password
3. **관리 (Admin)** → **사진 폴더 (Roots)** → **새 폴더 추가**
4. Sample-scan 200 → confirm via **색인 (Indexing)** tab → full scan

### Folder permissions (required)

The systemd `$USER` account needs read access to the photo folder. For
write ops (rotate / delete / trash):

| Goal | Permission | Command |
|---|---|---|
| Index + thumbs only | Read on the top dir | `sudo chmod 755 /srv/photos` |
| + Rotate / delete / trash | Read all + write on dirs | `sudo chown -R $USER:$USER /srv/photos && sudo chmod -R u+rwX,g+rX,o+rX /srv/photos` |

The capital `X` in `u+rwX` adds traverse on directories only (not exec
on regular files). exiftool needs **directory write** to create its
`<file>_exiftool_tmp` companion during rotation.

### 10) (optional) Trigger auto-classification

If step 7 ran, admin → **ML 자동 분류** card → enqueue with `objects`
first, then `embedding` + `faces`.

### 11) (optional) Add family users

관리 → **사용자**. Leave 관리자 unchecked for view-only family accounts.

### 12) (optional) Expose externally

nginx / Caddy / Traefik in front of `localhost:8888`, or Tailscale /
WireGuard. Session-cookie auth survives any standard LB.

---

Post-install ops are in the README's
[Post-install](../../README.md#post-install) section.

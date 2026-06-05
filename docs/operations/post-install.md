# 설치 후 운영

> 한국어 / [English](#english)

> [← README로 돌아가기](../../README.md)

설치는 OS별로 다르지만 운영(코드 업데이트 / 워처 / 백업 / 트러블슈팅)은
대부분 공통입니다. 명령은 두 종류를 함께 보여줍니다:

- **Linux / Synology** (systemd 기반) — `sudo systemctl ...`
- **Windows** (개발용 PowerShell) — `.\scripts\myphotos.ps1 ...` (start/stop/restart/status)

## 코드 업데이트

변경이 없는 단계는 no-op이라 매번 그대로 써도 부작용 없습니다.

**Linux / Synology**

```bash
cd ~/myphotos && git pull \
  && uv pip install --python .venv/bin/python -e . \
  && .venv/bin/python -m alembic upgrade head
sudo systemctl restart myphotos-api myphotos-worker myphotos-ml-worker myphotos-watcher
```

활성화하지 않은 유닛이 있으면 그 토큰은 빼세요 — 존재하지 않는 유닛
재시작 시 에러. (예: ML 워커/watcher 안 켰으면 `myphotos-api myphotos-worker`만)

**Windows (PowerShell)**

```powershell
cd $env:USERPROFILE\myphotos
git pull
uv pip install --python .venv\Scripts\python.exe -e .
.\.venv\Scripts\python.exe -m alembic upgrade head
.\scripts\myphotos.ps1 restart
```

`myphotos.ps1 restart`는 좀비 워커 정리까지 같이 — 옛 PowerShell 터미널이
백그라운드에서 남아있던 경우도 한 번에 정리합니다.

### 단계별로 (각 단계가 언제 필요한지)

| 단계 | 명령 (Linux / Windows) | 필요한 때 |
| --- | --- | --- |
| 1. 코드 받기 | `git pull` | 항상 |
| 2. 의존성 동기화 | `uv pip install --python .venv/bin/python -e .` / `uv pip install --python .venv\Scripts\python.exe -e .` | `pyproject.toml` 변경 시 (새 라이브러리/버전 핀 등) |
| 3. DB 마이그레이션 | `.venv/bin/python -m alembic upgrade head` / `.\.venv\Scripts\python.exe -m alembic upgrade head` | `alembic/versions/` 에 새 파일 추가 시 |
| 4. 서비스 재시작 | `sudo systemctl restart myphotos-*` / `.\scripts\myphotos.ps1 restart` | 코드/설정/스키마 어떤 것이든 바뀌었으면 |

어떤 단계가 진짜 필요했는지는 `git diff --stat HEAD@{1}` 으로 한 번에 보입니다.

### 동작 검증

**Linux / Synology**

```bash
sudo systemctl status myphotos-api myphotos-worker myphotos-ml-worker
```

```bash
curl -s http://localhost:8888/healthz | python3 -m json.tool
```

```bash
sudo journalctl -u myphotos-api -n 20 --no-pager
```

**Windows**

```powershell
.\scripts\myphotos.ps1 status
```

```powershell
Invoke-RestMethod http://localhost:8888/healthz | ConvertTo-Json -Depth 4
```

로그는 각 `run-*.ps1` 터미널 창에서 직접 (minimised 창은 작업 표시줄에서
클릭해 펼침).

`/healthz` 응답의 `version` 이 새 값으로 바뀌고, 모든 컴포넌트가
`running`이면 성공.

### 브라우저 캐시

프론트(`index.html`, `admin.html`) 변경된 commit이 섞여있는데도 UI가
그대로면 브라우저 캐시 때문입니다 — 강제 새로고침 (`Ctrl+Shift+R`,
모바일은 주소창 당겨서 새로고침).

### 외부 바이너리 업데이트 (드물게)

`exiftool`/`ffmpeg` 새 버전을 받으려면:

**Linux / Synology**

```bash
./scripts/install-vendor-linux-x64.sh
sudo systemctl restart myphotos-worker
```

**Windows** — scoop으로 깐 경우 자동 업데이트:

```powershell
scoop update exiftool ffmpeg
.\scripts\myphotos.ps1 restart
```

vendor에 수동 배치한 경우엔 [Windows 설치 가이드](../install/windows.md#exiftool--ffmpeg-raw--heic--동영상-썸네일용)의 다운로드 단계 다시.

ML 모델은 한 번 받으면 거의 갱신 안 되지만 새 모델 commit이 있으면:

```bash
./scripts/install-ml-models.sh                            # Linux/Synology
sudo systemctl restart myphotos-ml-worker
```

```powershell
bash ./scripts/install-ml-models.sh                       # Windows (Git Bash)
.\scripts\myphotos.ps1 restart
```

### 롤백

뭐가 잘못된 것 같으면 이전 commit으로 되돌리기.

먼저 직전 commit 해시 확인:

```bash
git log --oneline -10
```

원하는 해시로 리셋하고 의존성/스키마 정리 (스키마 downgrade는 정말
스키마도 되돌릴 때만):

**Linux / Synology**

```bash
git reset --hard <hash>
uv pip install --python .venv/bin/python -e .
.venv/bin/python -m alembic downgrade -1
sudo systemctl restart myphotos-api myphotos-worker myphotos-ml-worker
```

**Windows**

```powershell
git reset --hard <hash>
uv pip install --python .venv\Scripts\python.exe -e .
.\.venv\Scripts\python.exe -m alembic downgrade -1
.\scripts\myphotos.ps1 restart
```

⚠️ `alembic downgrade` 는 데이터 손실 가능성이 있는 마이그레이션이면
실패할 수 있습니다. 그땐 백업(`scripts/backup-db.sh` 로 미리 떠둔
파일)을 복원하는 게 안전합니다.

### 정기 백업

**Linux / Synology** — DSM **제어판 → 작업 스케줄러 → 사용자 정의 스크립트** 에 매일:

```bash
/var/services/homes/<user>/myphotos/scripts/backup-db.sh
```

**Windows** — Task Scheduler에 등록:

```powershell
# Git Bash 호출로 wrapper:
"C:\Program Files\Git\bin\bash.exe" -lc "cd /c/Users/scsung/myphotos && ./scripts/backup-db.sh"
```

또는 PowerShell 한 줄 백업 (SQLite 단순 복사):

```powershell
Copy-Item $env:USERPROFILE\myphotos\data\catalog.db $env:USERPROFILE\myphotos\data\backups\catalog-(Get-Date -f yyyyMMdd-HHmmss).db
```

`data/backups/` 에 최근 14개 자동 보관됩니다 (Linux 스크립트).

## 사진 폴더에서 직접 파일을 옮기거나 지우면 어떻게 되나

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

### 실시간 감지 (watchdog) — 선택적 활성화

기본은 daily 풀스캔 + 수동 트리거. 변경을 즉시 반영하고 싶으면 별도
워처 서비스를 켤 수 있습니다. inotify로 root를 구독하고, 변경 이벤트가
30초 동안(설정 가능) 잠잠해지면 그 root에 `discover_root` 잡을 자동
enqueue합니다.

켜는 법 (Linux / Synology):

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
```

```bash
sudo systemctl start  myphotos-watcher
```

```bash
sudo journalctl -u myphotos-watcher -f
```

inotify watch 한도 (10만+ 폴더면 필요):

```bash
echo "fs.inotify.max_user_watches=524288" | sudo tee -a /etc/sysctl.conf
echo "fs.inotify.max_user_instances=512"  | sudo tee -a /etc/sysctl.conf
sudo sysctl -p
```

> ⚠️ **한계** — inotify는 호스트 OS 파일시스템 변경만 감지합니다.
> 외부에서 SMB로 접속해 변경하는 것은 DSM의 samba 데몬이 쓰는
> 것이므로 보통 잡힙니다. 외부 NAS의 NFS 마운트, S3FS 같은 가상
> 파일시스템은 못 잡습니다 — 그쪽은 daily 풀스캔이 백업입니다.

> **Windows**: watcher 서비스 별도 PowerShell 런처는 아직 없습니다.
> Daily 풀스캔 + 관리 UI의 수동 스캔 버튼으로 충분합니다. 필요해지면
> `.\scripts\run-watcher.ps1` 추가 가능 (현재 미제공).

### Watcher 동작 상태 확인

**1. systemd 단의 살아있음** — `Active: active (running)` 이어야 함:

```bash
sudo systemctl status myphotos-watcher
```

**2. 부팅 로그** — 구독한 root 수 / 도구 감지 / catch-up. 정상이면
`"watcher observer started"`, `"watcher: subscribed root id=1 (/volume1/photo)"`,
`"watcher: catch-up touched 1 root(s)"`가 떠야 함:

```bash
sudo journalctl -u myphotos-watcher -n 50 --no-pager
```

**3. 실시간 로그** — 파일 추가/변경 시 이벤트 흐름 보기. 사진 폴더에
파일 한 개 던지고 ~30초 후 `"watcher: enqueued discover_root for root id=N"`
떠야 정상:

```bash
sudo journalctl -u myphotos-watcher -f
```

**4. API에서 한 줄** — 별도 SSH 없이 확인 가능. `watcher` 블록의
`alive_at`(최근 heartbeat 시각), `age_seconds`, `stale`(true면
15초 이상 무응답), `watched_root_ids`, `pending_roots` 확인:

```bash
curl -s http://localhost:8888/healthz | python3 -m json.tool       # Linux
Invoke-RestMethod http://localhost:8888/healthz | ConvertTo-Json    # Windows
```

자주 막히는 케이스:

| 증상 | 원인 / 해결 |
| --- | --- |
| `watcher disabled in config (watcher.enabled=false)` 후 종료 | `config/local.toml`에 `[watcher] enabled = true` 추가 후 재시작 |
| `Active: active (running)` 인데 `/healthz` `stale: true` | 프로세스는 살았지만 dispatcher가 멈춤 — `journalctl -u myphotos-watcher --since "10 min ago"` 로 traceback 확인 |
| `schedule failed ... No space left on device` | `fs.inotify.max_user_watches` 한도 초과. 위 sysctl 명령으로 늘리기 |
| `watched_root_ids: []` | DB에 enabled root 없음. 관리 → 사진 폴더에서 enable, 또는 root 추가 |
| 이벤트 발생해도 `enqueued discover_root` 안 뜸 | (1) ignore 패턴에 걸림 (.tmp, @eaDir 등), (2) 30초 debounce 대기 중, (3) 기존 discover_root 잡 inflight 중 |

## 포트 변경

`config/local.toml`에:

```toml
[server]
port = 9000
```

그 후 API 재시작:

```bash
sudo systemctl restart myphotos-api          # Linux/Synology
```

```powershell
.\scripts\myphotos.ps1 restart                # Windows
```

`myphotos-api.service`의 ExecStart에 포트가 박혀 있다면 (Linux)
`./scripts/install-systemd.sh` 재실행.

## 로그 보기

**Linux / Synology**

```bash
sudo journalctl -u myphotos-api    -n 60 --no-pager
sudo journalctl -u myphotos-worker -f
```

**Windows** — 각 `run-*.ps1` 터미널 창에서 직접. 작업 표시줄에서
minimised된 PowerShell 창 클릭. 별도 파일로 빼고 싶으면 `run-*.ps1`을
`python ... 2>&1 | Tee-Object ...` 패턴으로 수정.

## 설정

운영 중 바꾸는 설정은 세 군데에 있습니다: **관리 → 설정 탭**(대부분, `config/local.toml`에 자동 기록), **`config/local.toml`** 직접 편집(일부 고급 항목), **systemd 유닛/환경변수**(HTTPS 관련).

> ⚠ `config/local.toml`을 손으로 고칠 땐 같은 `[section]`을 **두 번 쓰지 마세요** (중복 선언은 TOML 오류 → API 기동 실패). 키는 기존 섹션 안에 추가하고, 저장 전 검사:
> ```bash
> .venv/bin/python -c "import tomllib; tomllib.load(open('config/local.toml','rb')); print('OK')"
> ```
> 직접 편집은 `get_settings` 캐시 때문에 **재시작 후 반영**됩니다. 반면 관리 UI 저장은 캐시를 비워 **즉시 반영**됩니다.

### HTTPS 설정 (선택 — 권장)

기본은 `http://NAS:8888`(평문)으로 접속합니다. 다음의 경우 HTTPS가 필요/권장됩니다:

- **외부(인터넷)에서 접속** — 비밀번호·세션 쿠키가 평문으로 흐르지 않게.
- **PWA(홈 화면 앱)의 오프라인 캐시(서비스워커)** — 보안 컨텍스트(HTTPS 또는 `localhost`)에서만 동작합니다. 평문 `http://NAS:8888`에선 서비스워커가 등록되지 않습니다. (반응형 UI와 iOS "홈 화면에 추가" 전체화면 실행은 HTTP에서도 됨.)
- **지도·사진 GPS의 "현재 위치" 버튼** — 브라우저 위치 API도 보안 컨텍스트 전용이라 HTTP에선 실패합니다. (EXIF GPS로 지도 표시·지도 클릭 GPS 편집은 HTTP에서도 정상 — *기기의 현재 위치* 따오기만 제한.)

방법 (택1):

**A. Synology DSM 리버스 프록시 + Let's Encrypt** — NAS만으로 (도메인/DDNS 필요)
1. 제어판 → 보안 → 인증서 → 추가 → Let's Encrypt 인증서 발급.
2. 제어판 → 로그인 포털 → 고급 → **리버스 프록시** → 생성: 소스 `HTTPS` / `photos.example.com` / `443`, 대상 `HTTP` / `localhost` / `8888`.
3. 리버스 프록시 항목에 위 인증서를 지정 → `https://photos.example.com` 접속.

**B. Tailscale** — 도메인·포트 개방 불필요, 가장 간단 (내 기기끼리만 노출)
```bash
sudo tailscale serve --bg 8888
```
MagicDNS + 자동 인증서로 HTTPS가 바로 됩니다. (버전에 따라 문법 상이 — `tailscale serve status`로 확인.)

**C. Caddy / nginx 리버스 프록시** — 일반 Linux / Docker
Caddy 예(`Caddyfile`): `photos.example.com { reverse_proxy localhost:8888 }` → Let's Encrypt 자동 발급·갱신.

HTTPS(리버스 프록시)를 붙인 뒤에는 추가로:

1. **보안 쿠키 켜기** — systemd 유닛(또는 `.env`)에 환경변수 추가 → 세션 쿠키에 `Secure` + `SameSite=strict` 적용:
   ```
   MYPHOTOS_SECURE_COOKIE=1
   ```
2. **프록시 뒤 실제 IP 인식** — GeoIP/로그 기록이 클라이언트 IP를 제대로 보게 `config/local.toml`:
   ```toml
   [security]
   trust_proxy_xff = true
   ```
3. **(선택) HSTS** — HTTPS에서만 켜세요 (HTTP에선 무시됨):
   ```toml
   [security]
   hsts = true
   ```

> 자가서명 인증서는 브라우저 경고 + 서비스워커 미동작이라 PWA 오프라인엔 부적합. LAN 전용이면 평문 `http`도 무방합니다.

### 접근 제어 / 방화벽

앱에 내장된 보안 기능 (대부분 기본 On / 안전):

| 기능 | 설정 위치 | 기본값 / 메모 |
| --- | --- | --- |
| **보안 HTTP 헤더** | 자동 | `X-Content-Type-Options:nosniff`, `Referrer-Policy` 항상; `X-Frame-Options:SAMEORIGIN`(`[security] frame_deny`, 기본 on); HSTS는 위 참고 |
| **로그인 IP 레이트리밋** | 자동 | 5분간 8회 실패 시 429 (in-memory) |
| **계정 잠금** | `[security] lockout_threshold` / `lockout_minutes` | 연속 10회 실패 → 15분 잠금. 0이면 끔. 로그인 성공/실패는 **활동 로그**에 기록됨 |
| **GeoIP 국가 차단** | **관리 → 설정 → 보안 (국가 차단)** | 기본 off. `pip install geoip2` + GeoLite2-Country.mmdb 필요. `allow`=목록만 허용 / `block`=목록만 차단. **사설/LAN IP는 항상 허용**, DB·패키지 없으면 자동 비활성(fail-open) |

> 계정 잠금 해제(관리자가 잠긴 경우):
> ```bash
> .venv/bin/python -c "import sqlite3; from app.paths import DB_PATH; c=sqlite3.connect(DB_PATH); c.execute('UPDATE users SET locked_until=NULL, failed_login_count=0'); c.commit(); print('unlocked')"
> ```

**OS/네트워크 방화벽** (앱 외부):
- 외부에 노출하는 포트는 **최소화** — 리버스 프록시면 `443`만, 직결이면 앱 포트만 포트포워딩.
- **SMB(445)** (PhotoSync 업로드용 공유)는 **LAN 전용**으로 두고 인터넷에 열지 마세요.
- GeoIP `allow`는 "목록 외 전부 차단"이라 **자기 차단 위험** — 외국 차단이 목적이면 `block` 모드를 권장. (SSH는 영향 없으니 막혀도 `geoip_mode="off"` 후 복구 가능.)

### 관리 페이지에서 설정할 수 있는 항목 (관리 → 설정 탭)

저장 시 `config/local.toml`에 기록되고 **즉시 반영**됩니다. "워커/API 재시작 필요" 뱃지가 붙은 항목만 저장 후 `sudo systemctl restart …`가 추가로 필요합니다.

| 섹션 | 주요 항목 | 재시작 |
| --- | --- | --- |
| **앱 / 표시** | 앱 이름, 표시 시간대(`display_timezone`), 기본 UI 언어 | 불필요 |
| **지도 라이트박스** | 단일 마커 클릭 시 근처 사진 반경/개수 | 불필요 |
| **워커 (색인)** | `concurrency`(동시 처리), 폴링 주기, 잡 리스 시간 | **워커** |
| **썸네일** | JPEG 품질 (새로 생성분에만 적용) | 불필요 |
| **EXIF 추출** | 추출기 순서(`pillow, exiftool`) | 불필요 |
| **로깅** | 로그 레벨 | **API+워커** |
| **스캐너** | 색인 제외 폴더/파일, 인덱싱 확장자(이미지/동영상) | 불필요 |
| **보안 (국가 차단)** | GeoIP `geoip_mode` / `geoip_countries` / `geoip_db_path` | 불필요(핫리로드) |

> 여기 없는 고급 항목(서버 host/port, `security.secret_key`, `trust_proxy_xff`, `hsts`, 잠금 임계값, paths 등)은 재시작이 필요하거나 연결을 끊을 수 있어 UI에서 빼고 `config/local.toml`로 직접 설정합니다.

### OCR 텍스트 검색 (선택)

사진 속 글자(스크린샷·문서·간판·영수증 등)를 추출해 **검색에 포함**시키는 기능입니다. 객체/얼굴 분류처럼 **ML 워커가 처리하는 opt-in 단계**라, 켠 사진만 동작합니다. 엔진은 RapidOCR(onnxruntime, PyTorch 불필요).

1. **패키지 설치** — `rapidocr`(v3)는 `[ocr] lang`(기본 `korean`) 모델을 **자동 다운로드**합니다(수동 모델 파일 불필요). numpy를 다시 빌드하지 않게 설치된 버전으로 고정:
   ```bash
   uv pip install --python .venv/bin/python rapidocr "numpy==$(.venv/bin/python -c 'import numpy; print(numpy.__version__)')"
   ```
   헤드리스 NAS면 GUI용 opencv가 `libGL.so.1`을 못 찾으니 headless로 교체:
   ```bash
   uv pip uninstall --python .venv/bin/python opencv-python
   ```
   ```bash
   uv pip install --python .venv/bin/python opencv-python-headless "numpy==$(.venv/bin/python -c 'import numpy; print(numpy.__version__)')"
   ```
   확인: `.venv/bin/python -c "import cv2, rapidocr; print('ok')"`
2. **언어** — 기본 `[ocr] lang = "korean"`. 다른 언어면 `config/local.toml`의 `[ocr]`에서 변경(`japan`/`ch`/`en`…). 모델은 첫 OCR 때 ModelScope에서 자동으로 받습니다(인터넷 필요, 1회).
3. **워커 재시작** 후 **관리 → 색인 → ML 자동 분류**에서 **OCR (텍스트 검색용)** 체크 → 실행.
   ```bash
   sudo systemctl restart myphotos-ml-worker
   ```
4. 완료되면 추출 텍스트가 **FTS 검색 인덱스에 자동 반영**됩니다. 이미지가 아니거나 글자가 없으면 각각 `skipped`/`empty`로 저장돼 검색엔 영향 없음.

**OCR된 사진 활용:**
- **검색**: 갤러리 검색창에 사진 속 글자(3자 이상 권장 — FTS trigram)를 입력하면, 파일명·태그와 함께 OCR 텍스트도 통합 매칭됩니다.
- **필터**: 검색바의 `🔤 텍스트` 셀렉트 → **있음**(`ok`) / **없음**(`empty`) 으로 OCR 결과만 골라보기.
- **확인**: 라이트박스 정보 패널의 **"텍스트 (OCR)"** 섹션에서 실제 인식된 글자를 볼 수 있습니다.

**새 사진 자동 OCR:** 초기 백로그를 위처럼 수동으로 돌린 뒤, **관리 → 설정 → ML 자동 분류 → `auto_enqueue` 켜기**(+ 워커 재시작) 하면, 이후 들어오는 사진은 색인되며 ML 분류 + OCR이 자동으로 큐에 들어갑니다.

진행/상태 확인:
```bash
.venv/bin/python -c "import sqlite3; from app.paths import DB_PATH; c=sqlite3.connect(DB_PATH); print(list(c.execute('select ocr_status, count(*) from photos group by ocr_status')))"
```
`pending`이 줄고 `ok`(글자 있음)/`empty`(글자 없음)가 늘면 정상입니다.

> CPU를 많이 쓰는 일괄 작업이라 대량이면 시간이 걸립니다. `[ocr] min_score`(신뢰도 컷)·`max_chars`(저장 길이 상한)로 조절. 엔진 미설치 시 잡은 **pending 대기**하다 설치 후 자동 재개됩니다. RapidOCR의 "text detection result is empty" 로그는 글자 없는 사진이라는 정상 신호입니다.
>
> **구버전 `rapidocr_onnxruntime`(v1)** 도 폴백 지원하지만 한글은 모델을 수동으로 넣어야 합니다(`[ocr] rec_model_path` + `rec_keys_path`에 한국어 rec ONNX + dict). 가능하면 위 v3를 쓰세요.

## 문제 해결

| 증상 | 확인 / 해결 |
| --- | --- |
| 사진 폴더 root가 **`접근 불가`** (Synology) | Synology Photos가 만든 폴더는 보통 `d---------+` (ACL 전용)이라 systemd가 실행하는 `$USER` 계정으론 못 읽음. `ls -la /volume1/photo`로 확인하고 `sudo chmod 755 /volume1/photo` (또는 `synoacltool` ACL 추가). |
| 회전·삭제 시 **`Permission denied`** / **`Error creating file: ..._exiftool_tmp`** | 디렉토리 쓰기 권한 부족. exiftool은 같은 폴더에 임시 파일을 만들고, 삭제는 폴더에서 파일 entry를 지워야 함. 트리 전체 `chmod -R u+rwX,g+rX,o+rX` 적용. `ls -ld /volume1/photo/2024년사진/`로 디렉토리에 `w`가 있는지 확인. |
| 삭제한 사진이 **새로고침하면 다시 나타남** | 휴지통 이동이 실패했는데도 (권한 부족 등) UI에서 사라졌다가, 다음 스캐너 패스가 원래 폴더의 파일을 발견하고 `status='active'`로 부활시킴. 실패 사유가 alert로 뜨면 그 원인부터 해결. |
| 잡 큐에 잡이 계속 쌓이고 줄지 않음 | 워커가 죽었거나 이전 잘못된 잡들이 큐를 막고 있을 수 있음. 워커 상태 확인 → 죽었으면 로그 확인. 큐 비우려면 관리 → 색인 → 잡 큐 → "대기·실패 잡 비우기" 또는 CLI `curl -X POST http://localhost:8888/api/admin/jobs/purge -H "Content-Type: application/json" -d '{"include_running":true}'`. |
| 타임라인이 비거나 500 오류 | `alembic current`가 `(head)`인지 확인. 아니면 `alembic upgrade head` 후 재시작 |
| 색인이 너무 느림 | 관리 → 설정 → 워커 → `concurrency` 조정. HDD면 3~4가 더 빠를 수 있음 |
| 워커 좀비 (status에 두 개 떠 있음) | Linux: `ps -ef \| grep app.worker`; systemd 외부 프로세스 `kill`. Windows: `.\scripts\myphotos.ps1 status`가 ⚠로 표시; `.\scripts\myphotos.ps1 stop` → `start` |
| ML 워커가 active되자마자 죽음 | `journalctl -u myphotos-ml-worker -n 30`에 `model missing` 있으면 `./scripts/install-ml-models.sh` 미실행. 받은 후 재시작 |
| ML 분류 잡 다수가 failed | 모델 출력 형식이 코드 기대와 다른 변종일 수 있음. 위 로그의 traceback과 함께 이슈 등록 |
| admin 비밀번호 잊음 | `.venv/bin/python -c "from app.auth import hash_password; print(hash_password('새비번'))"` → 출력 해시를 sqlite3로 `UPDATE users SET password_hash='<해시>' WHERE username='admin';` |

---

# English

## Post-install operations

> [← Back to README](../../README.md)

Install differs by OS, but operations are mostly common. Commands below
show both forms where they differ:

- **Linux / Synology** (systemd) — `sudo systemctl ...`
- **Windows** (dev PowerShell) — `.\scripts\myphotos.ps1 ...`
  (start/stop/restart/status)

### Updating the code

Pre-restart steps (no-op when nothing changed):

**Linux / Synology**

```bash
cd ~/myphotos && git pull \
  && uv pip install --python .venv/bin/python -e . \
  && .venv/bin/python -m alembic upgrade head
sudo systemctl restart myphotos-api myphotos-worker myphotos-ml-worker
```

**Windows (PowerShell)**

```powershell
cd $env:USERPROFILE\myphotos
git pull
uv pip install --python .venv\Scripts\python.exe -e .
.\.venv\Scripts\python.exe -m alembic upgrade head
.\scripts\myphotos.ps1 restart
```

`myphotos.ps1 restart` also sweeps zombie workers from terminals that
were closed without Ctrl+C.

### Health check

```bash
sudo systemctl status myphotos-api myphotos-worker          # Linux/Synology
curl -s http://localhost:8888/healthz | python3 -m json.tool
```

```powershell
.\scripts\myphotos.ps1 status                                # Windows
Invoke-RestMethod http://localhost:8888/healthz | ConvertTo-Json -Depth 4
```

### Troubleshooting

Most issues map to one of: missing exiftool/ffmpeg, missing pillow-heif
for HEIC, zombie worker process holding a stale tool cache, or
permission gaps on the photo root. See [Windows install
guide](../install/windows.md#트러블슈팅) for the full Windows-specific
playbook covering all four cases.

For Synology / Linux:

| Symptom | Check / fix |
| --- | --- |
| Root row shows **`접근 불가`** (no access) | Synology Photos folders are usually `d---------+` (ACL-only) and unreadable by the systemd `$USER`. `ls -la /volume1/photo` to confirm, then `sudo chmod 777 /volume1/photo` (or a `synoacltool` ACL entry). |
| Queue keeps growing | Worker may be dead, or stale jobs blocking. Check `sudo systemctl status myphotos-worker`; if running, purge via Admin → 색인 → 잡 큐 → "대기·실패 잡 비우기", or `curl -X POST http://localhost:8888/api/admin/jobs/purge -H "Content-Type: application/json" -d '{"include_running":true}'`. |
| Empty timeline or 500 errors | `alembic current` should end in `(head)`; if not, `alembic upgrade head` and restart |
| Slow indexing | 관리 → 설정 → worker → `concurrency`. HDD storage often goes faster at 3–4 than 6+ |
| Forgot admin password | `.venv/bin/python -c "from app.auth import hash_password; print(hash_password('new_pw'))"`, then `sqlite3 data/catalog.db "UPDATE users SET password_hash='<hash>' WHERE username='admin';"` |

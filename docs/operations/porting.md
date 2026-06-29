# 다른 호스트로 이전 (재인덱싱 없이)

> 한국어 / [English](#english)

> [← README로 돌아가기](../../README.md)

다른 NAS / Linux 박스 / Windows 머신으로 이전해도 **재인덱싱 없이** 그대로
사용 가능합니다. 썸네일은 SHA-256으로 주소되고, `photos.rel_path`는 root
기준 상대 경로(POSIX/NFC)로 저장되어 있어 호스트별로 바뀌는 건
`roots.abs_path` 하나뿐입니다.

명령은 두 종류를 함께 보여줍니다:

- **Linux / Synology** (systemd 기반)
- **Windows** (개발용 PowerShell + `myphotos.ps1`)

## 1) 원본 호스트 — 정합성 있는 스냅샷

**Linux / Synology**

```bash
sudo systemctl stop myphotos-api myphotos-worker
```

```bash
sqlite3 ~/myphotos/data/catalog.db ".backup ~/myphotos/data/catalog.db.snapshot"
```

**Windows**

```powershell
.\scripts\myphotos.ps1 stop
```

```powershell
.\.venv\Scripts\python.exe -c "import sqlite3; sqlite3.connect(r'data\catalog.db').execute('VACUUM INTO ?', [r'data\catalog.db.snapshot'])"
```

> WAL 모드라 서비스 정지 없이 그대로 `data/`를 복사하면
> `catalog.db-wal`이 어중간한 상태일 수 있습니다. 정지 → backup → 전송이
> 안전합니다. 외부 DB(MariaDB / PostgreSQL) 백엔드라면 [external-db.md의 백업](external-db.md#4-백업)
> 절을 따라 `mysqldump` 또는 `pg_dump`를 뽑으세요.

## 2) 새 호스트로 전송

`data/` 통째로 + `config/local.toml` 두 가지만 옮기면 됩니다.

**Linux / Synology → Linux / Synology**

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

**Windows → Windows (또는 Windows ↔ Linux)**

PowerShell에서 `robocopy`(로컬 디스크간) 혹은 `scp`(SSH 가능한 새 호스트
대상):

```powershell
# 같은 머신에서 다른 드라이브로 (예: D:\myphotos)
robocopy $env:USERPROFILE\myphotos\data D:\myphotos\data /MIR
Copy-Item $env:USERPROFILE\myphotos\config\local.toml D:\myphotos\config\local.toml -Force
```

```powershell
# Windows → Linux NAS (OpenSSH 클라이언트 필요 — Windows 10+ 기본 포함)
scp -r $env:USERPROFILE\myphotos\data\* user@newnas.local:~/myphotos/data/
scp $env:USERPROFILE\myphotos\config\local.toml user@newnas.local:~/myphotos/config/local.toml
```

`thumbs/` 디렉토리가 상당히 클 수 있으니(10만 장이면 수 GB) 처음 한 번은
배경에서 돌리고 진행률을 확인하세요. `--mirror` / `/MIR`은 양쪽을 정확히
같은 상태로 만들므로 재실행해도 누락 파일을 채워 넣을 수 있습니다.

## 3) 새 호스트 — 셋업

**Linux / Synology**

```bash
# 코드는 새로 clone (vendor/와 .venv는 OS별이므로 재생성)
git clone git@github.com:saintsc-ai/MyPhotos.git ~/myphotos

# data/ 와 config/local.toml은 위 2)에서 이미 자리잡고 있음
cd ~/myphotos
./scripts/bootstrap.sh                       # Python venv
./scripts/install-vendor-linux-x64.sh        # exiftool / ffmpeg (OS별 바이너리)
./scripts/install-systemd.sh
sudo systemctl enable myphotos-api myphotos-worker
```

```bash
sudo systemctl start myphotos-api myphotos-worker
```

**Windows**

```powershell
# 코드는 새로 clone
cd $env:USERPROFILE
git clone https://github.com/saintsc-ai/MyPhotos.git myphotos
cd myphotos

# data/ 와 config/local.toml은 위 2)에서 이미 자리잡고 있음
.\scripts\bootstrap.ps1
# exiftool / ffmpeg 는 windows.md 안내대로 Scoop 또는 수동 설치
.\scripts\myphotos.ps1 start
```

[설치 가이드 (windows.md)](../install/windows.md) 의 외부 바이너리
설치 단계만 따라 하면 됩니다 — DB와 썸네일은 이미 옮긴 상태라서 색인이
다시 돌지 않습니다.

## 4) 사진 폴더 경로 갱신

원본 NAS에서 `/volume1/photo`였던 root가 새 호스트에서는
`/mnt/data/photos`(Linux) 또는 `D:\Photos`(Windows) 처럼 바뀌었을 수
있습니다. 관리 페이지에서 수정:

1. 브라우저로 `http://새-호스트:8888/admin.html` 접속
2. **사진 폴더** 탭 → 해당 루트 행의 **`경로`** 버튼 클릭
3. 새 절대 경로 입력 → 저장

루트의 **라벨은 그대로 유지**되고, `photos.rel_path`(상대 경로)도 그대로이므로
이 한 가지만 바꾸면 모든 사진이 다시 연결됩니다.

또는 curl로 (Linux / Synology):

```bash
curl -b cookies -X PATCH http://newnas:8888/api/admin/roots/1 \
  -H "Content-Type: application/json" \
  -d '{"abs_path":"/mnt/data/photos"}'
```

PowerShell (Windows):

```powershell
Invoke-RestMethod -Uri http://localhost:8888/api/admin/roots/1 `
  -Method Patch -WebSession $sess `
  -ContentType 'application/json' `
  -Body '{"abs_path":"D:\\Photos"}'
```

또는 **서버를 띄우지 않고** CLI로 (서비스 정지 상태에서 실행, 새 경로의
파일 존재까지 샘플 검증):

```bash
python -m app.tools.cutover --list                              # 현재 루트
python -m app.tools.cutover --map "D:/Photos=/volume1/photos"   # 미리보기
python -m app.tools.cutover --map "D:/Photos=/volume1/photos" --apply
```

> 강력한 GPU PC에서 초기 백로그를 색인한 뒤 NAS로 적재하는 전체 흐름은
> [bulk-index-gpu.md](bulk-index-gpu.md) 참고.

## 5) 검증

관리 → **색인** 탭에서 EXIF/썸네일 진행률이 이전 호스트의 값과 동일한지
확인. 만약 일부가 `missing`으로 바뀌었다면 그건 root 안 내부 폴더 구조가
달라진 사진들 — 디스커버리를 한 번 돌리면(`시험` 버튼) `missing` 또는
`active`로 재정리됩니다.

`/healthz`로 백엔드와 외부 도구 인식 여부를 한 번에 확인:

```bash
curl -s http://localhost:8888/healthz
```

`tools.exiftool` / `tools.ffmpeg` 가 `null`이 아니고 `db.backend`가
기대값(`sqlite` / `mysql` / `postgresql`)이면 정상.

## 옮기지 않는 것

| 항목 | 이유 |
| --- | --- |
| `vendor/<os-arch>/` | exiftool/ffmpeg는 OS별 바이너리. 새 호스트에서 `install-vendor-*.sh` 또는 Scoop으로 재설치 |
| `.venv/` | Python venv도 호스트별. `bootstrap.sh` / `bootstrap.ps1`이 새로 만듦 |
| `*.db-wal`, `*.db-shm` | WAL 부속 파일은 backup 명령 이후 자동 흡수됨 |

## 옮기지 않으면 일어나는 일

| 빠뜨림 | 결과 |
| --- | --- |
| `data/catalog.db` | 전부 재색인 (몇 시간) |
| `data/thumbs/` | DB는 살아있지만 모든 썸네일 재생성 |
| `data/session.secret` | 새 키 자동 생성 → 모든 사용자 재로그인 |
| `config/local.toml` | 기본값으로 동작 (secret_key는 자동 생성). 별도 튜닝은 다시 설정. 외부 DB(MariaDB / PostgreSQL) 백엔드라면 DSN도 다시 넣어야 함 |

---

## English

> [← back to README](../../README.md)

You can move to another NAS / Linux box / Windows machine **without
re-indexing**. Thumbnails are addressed by SHA-256, and
`photos.rel_path` is a root-relative POSIX/NFC path, so the only thing
that changes per host is `roots.abs_path`.

Commands are shown for both:

- **Linux / Synology** (systemd)
- **Windows** (dev PowerShell + `myphotos.ps1`)

## 1) Source host — consistent snapshot

**Linux / Synology**

```bash
sudo systemctl stop myphotos-api myphotos-worker
```

```bash
sqlite3 ~/myphotos/data/catalog.db ".backup ~/myphotos/data/catalog.db.snapshot"
```

**Windows**

```powershell
.\scripts\myphotos.ps1 stop
```

```powershell
.\.venv\Scripts\python.exe -c "import sqlite3; sqlite3.connect(r'data\catalog.db').execute('VACUUM INTO ?', [r'data\catalog.db.snapshot'])"
```

> Because the DB runs in WAL mode, just copying `data/` while the app
> is live can leave `catalog.db-wal` in an inconsistent state. Stop →
> backup → transfer is the safe path. On an external DB (MariaDB /
> PostgreSQL) backend, follow the matching `mysqldump` / `pg_dump`
> recipe in [external-db.md](external-db.md#4-backups).

## 2) Transfer to the new host

You only need two things: `data/` in full plus `config/local.toml`.

**Linux / Synology → Linux / Synology**

```bash
# fill in for your environment
NEW_HOST="newnas.local"          # new NAS host (or IP)
NEW_USER="$USER"                 # account name on the new NAS

# full data/ tree (catalog.db, thumbs/, session.secret, trash/, logs/)
rsync -aP ~/myphotos/data/ \
  "$NEW_USER@$NEW_HOST:~/myphotos/data/"

# host-specific config (includes secret_key — preserves existing sessions)
rsync -aP ~/myphotos/config/local.toml \
  "$NEW_USER@$NEW_HOST:~/myphotos/config/local.toml"
```

**Windows → Windows (or Windows ↔ Linux)**

In PowerShell, use `robocopy` (local-disk to local-disk) or `scp`
(target reachable over SSH):

```powershell
# Same machine, different drive (e.g. D:\myphotos)
robocopy $env:USERPROFILE\myphotos\data D:\myphotos\data /MIR
Copy-Item $env:USERPROFILE\myphotos\config\local.toml D:\myphotos\config\local.toml -Force
```

```powershell
# Windows → Linux NAS (needs OpenSSH client — bundled on Windows 10+)
scp -r $env:USERPROFILE\myphotos\data\* user@newnas.local:~/myphotos/data/
scp $env:USERPROFILE\myphotos\config\local.toml user@newnas.local:~/myphotos/config/local.toml
```

`thumbs/` can be sizeable (multi-GB for 100k photos), so run the first
pass in the background and watch the progress. `--mirror` / `/MIR` make
the run idempotent — re-running fills in anything that didn't make it
the first time.

## 3) New host — set up

**Linux / Synology**

```bash
# fresh clone (vendor/ and .venv are per-OS, so they're regenerated)
git clone git@github.com:saintsc-ai/MyPhotos.git ~/myphotos

# data/ and config/local.toml were placed in step 2
cd ~/myphotos
./scripts/bootstrap.sh                       # Python venv
./scripts/install-vendor-linux-x64.sh        # exiftool / ffmpeg (per-OS binaries)
./scripts/install-systemd.sh
sudo systemctl enable myphotos-api myphotos-worker
```

```bash
sudo systemctl start myphotos-api myphotos-worker
```

**Windows**

```powershell
# fresh clone
cd $env:USERPROFILE
git clone https://github.com/saintsc-ai/MyPhotos.git myphotos
cd myphotos

# data/ and config/local.toml were placed in step 2
.\scripts\bootstrap.ps1
# exiftool / ffmpeg per the install guide (Scoop or manual)
.\scripts\myphotos.ps1 start
```

Follow the external-binary section in
[the Windows install guide](../install/windows.md). The DB and
thumbnails are already in place, so no re-indexing kicks off.

## 4) Point the root at the new path

The root that was `/volume1/photo` on the original NAS might be
`/mnt/data/photos` (Linux) or `D:\Photos` (Windows) on the new host.
Update it from the admin page:

1. Open `http://new-host:8888/admin.html`
2. **Photo folders** tab → **`Path`** button on the relevant row
3. Enter the new absolute path → Save

The root's **label stays the same**, and `photos.rel_path` (relative
to the root) doesn't change either, so this single edit reconnects
every photo.

Or via curl (Linux / Synology):

```bash
curl -b cookies -X PATCH http://newnas:8888/api/admin/roots/1 \
  -H "Content-Type: application/json" \
  -d '{"abs_path":"/mnt/data/photos"}'
```

PowerShell (Windows):

```powershell
Invoke-RestMethod -Uri http://localhost:8888/api/admin/roots/1 `
  -Method Patch -WebSession $sess `
  -ContentType 'application/json' `
  -Body '{"abs_path":"D:\\Photos"}'
```

## 5) Verify

In **Admin → Indexing**, check that the EXIF / thumbnail progress
matches the old host. Anything that flipped to `missing` is a photo
whose intra-root folder structure changed — one discovery run
(the **Test** button) re-sorts those into `missing` or `active`.

`/healthz` confirms backend and external tools in one shot:

```bash
curl -s http://localhost:8888/healthz
```

Healthy when `tools.exiftool` / `tools.ffmpeg` aren't `null` and
`db.backend` matches what you expect (`sqlite` / `mysql` / `postgresql`).

## Things NOT to copy

| Item | Reason |
| --- | --- |
| `vendor/<os-arch>/` | exiftool/ffmpeg are per-OS binaries — install via `install-vendor-*.sh` or Scoop on the new host |
| `.venv/` | Python venvs are per-host. `bootstrap.sh` / `bootstrap.ps1` recreates it |
| `*.db-wal`, `*.db-shm` | WAL side-files get absorbed by the backup command |

## What happens if you skip a piece

| Missing | Result |
| --- | --- |
| `data/catalog.db` | Full re-index (hours) |
| `data/thumbs/` | DB intact but every thumbnail regenerates |
| `data/session.secret` | New key generated → everyone logs in again |
| `config/local.toml` | Falls back to defaults (secret_key auto-generated). Tuning needs to be redone. External-DB users (MariaDB / PostgreSQL) also have to re-enter the DSN |

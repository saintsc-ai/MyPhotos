# Docker 설치 가이드

> 한국어 / [English](#english)

> [← README로 돌아가기](../../README.md)

NAS에 Python/uv/exiftool/ffmpeg를 직접 설치하지 않고 컨테이너로 굴리고
싶을 때. 단일 이미지로 API + 인덱싱 워커 + (선택) ML 워커 3개 컨테이너를
띄웁니다. DSM Container Manager / 일반 Linux + Docker / Windows + Docker Desktop
모두 동일 절차.

## 0) 사전 준비

- Docker 20.10+ / Docker Compose v2 (DSM 7.2+는 "Container Manager"
  패키지에 둘 다 포함)
- **Git** — DSM 패키지 센터에서 "Git Server" 설치 (코드 clone에 필요).
  검증은 `git --version`. Synology에서의 자세한 설치는
  [Synology 가이드의 사전 준비](synology.md#사전-준비) 참고.
- 사진 폴더 경로(호스트 측) — 예: `/volume1/photo`
- runtime 데이터를 둘 호스트 경로 — 예: `/volume1/docker/myphotos/data`.
  **미리 만들어 두고, 컨테이너가 쓸 수 있게 소유권까지 맞춰 두십시오**
  (구체적인 명령은 아래 1) 단계 참고). Docker 바인드 마운트는 호스트
  경로가 없으면 자동 생성 안 하고 `Bind mount failed: ... does not
  exists` 로 떨어지고, **설령 자동 생성돼도 root 소유**라 비루트
  (`APP_UID`, 기본 1000)로 도는 컨테이너가 `/app/data`에 못 써서 api
  컨테이너가 부팅 직후 죽습니다 (→ worker가 `Container "..." is
  unhealthy`로 떨어짐).

### DSM(시놀로지)에서 docker CLI가 안 잡힐 때

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
# A. v1 그대로 쓰기 (가장 빠름) — 이하 가이드의 모든
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

### Docker 소켓 권한 (Synology / Linux SSH 사용자)

SSH 사용자가 docker group 멤버가 아니면 `docker` / `docker-compose`
호출이 모두 `PermissionError: [Errno 13] Permission denied`로 실패합니다
(`/var/run/docker.sock` 접근 거부). 둘 중 하나:

**A. 매번 `sudo`로 — 한 번도 안 건드리고 바로 사용**

```bash
sudo docker compose pull
sudo docker compose up -d
```

이 가이드의 모든 `docker compose ...` 명령 앞에 `sudo`를 붙여 읽으세요.

**B. 사용자를 docker group에 추가 — 한 번만**

```bash
sudo synogroup --add docker $USER
```

DSM은 표준 `usermod -aG docker $USER` 대신 `synogroup`을 씁니다.
적용을 위해 SSH 세션 종료 후 재접속 → 이후 `sudo` 없이 동작.

## 1) 코드 받기 + 환경 파일 작성

```bash
cd ~
git clone https://github.com/saintsc-ai/MyPhotos.git myphotos
cd ~/myphotos
cp .env.example .env
# 편집: PHOTO_ROOT, DATA_DIR, API_PORT, APP_UID/APP_GID
```

- `PHOTO_ROOT` — 사진 폴더 절대 경로 (컨테이너에서 `/photos:ro`로 마운트됨)
- `DATA_DIR`   — 카탈로그 DB / 썸네일 / 로그가 들어갈 호스트 경로
- `APP_UID/GID` — **빌드 타임 인자**라 GHCR 이미지(기본)에는 반영되지
  않습니다. 기본값(1000) 그대로 두고, 아래 ⚠ 박스대로 `DATA_DIR`·`config/`
  chown + 사진 폴더 읽기 권한으로 맞추세요. 직접 빌드해 다른 UID로 돌릴
  때만 사진 파일 소유 계정의 `id -u` / `id -g`로 바꿉니다.

> ⚠ 컨테이너는 비루트 사용자(UID **1000**)로 돌기 때문에, 호스트에서
> 마운트하는 **두 경로를 그 UID 소유로** 맞춰야 합니다 — `DATA_DIR`
> (DB/썸네일 **쓰기**)과 `config/` (`default.toml` **읽기** +
> `local.toml` **쓰기**). 안 그러면 entrypoint의
> `mkdir -p /app/data/logs …`나 alembic의 `config/default.toml` 읽기가
> `Permission denied`로 죽고, api가 healthy가 못 돼 worker가
> `Container "…" is unhealthy`로 떨어집니다. `.env` 작성 직후 (필수):
>
> ```bash
> DATA_DIR=$(grep -E '^DATA_DIR=' .env | cut -d= -f2-)
> CONFIG_DIR=$(grep -E '^CONFIG_DIR=' .env | cut -d= -f2-)
> mkdir -p "$DATA_DIR"
> sudo chown -R 1000:1000 "$DATA_DIR" "${CONFIG_DIR:-./config}"
> ```
>
> `config/`는 git 체크아웃 안에 있어서, 나중에 `git pull`이 거기 파일을
> 갱신할 때 권한 충돌이 나면 `sudo git pull` 또는 위 chown을 다시 돌리면
> 됩니다 (git 소유권을 안 건드리려면 `sudo chmod -R a+rwX "${CONFIG_DIR:-./config}"`
> 로 대체 가능).
>
> 사진 폴더(`PHOTO_ROOT`)는 읽기 전용으로 마운트되지만 UID 1000이
> **읽을 수** 있어야 합니다. Synology Photos 기준으로:
>
> - **공유 공간**(`/volume1/photo`, 공유앨범)은 공유 폴더라 대개
>   `drwxrwxrwx+ root root`(0777)로 열려 있어 **권한 작업이 필요 없습니다.**
> - **개인 공간**(`/volume1/homes/<user>/Photos`)은 사용자별로 잠겨
>   있어(`d---------+`, ACL 전용) UID 1000이 못 읽습니다.
>
> 등록 전 `sudo docker-compose exec api ls /photos`로 확인하고, 파일이
> 안 보이거나 root가 `접근 불가`로 뜨면 그때만 읽기 권한을 여세요:
>
> ```bash
> PHOTO_ROOT=$(grep -E '^PHOTO_ROOT=' .env | cut -d= -f2-)
> sudo chmod -R a+rX "$PHOTO_ROOT"   # 그래도 안 열리면 chmod -R 777 또는 DSM ACL
> ```
>
> Synology 메타 폴더(`@eaDir`, `#recycle` 등)는 스캐너가 기본으로
> 건너뛰므로 따로 설정할 필요 없습니다.
>
> GHCR 이미지는 UID/GID **1000으로 고정 빌드**돼 있으므로 `APP_UID/GID`는
> 기본값(1000) 그대로 두고 `DATA_DIR`만 1000 소유로 만들면 됩니다. `.env`에서
> 그 값을 바꿔도 GHCR 이미지를 쓰는 한 런타임 UID는 1000이라 반영되지
> 않습니다 (다른 UID로 돌리려면 로컬 빌드 필요 — 아래 트러블슈팅
> "UID/GID를 바꿨는데 반영 안 됨" 참고).

## 2) 이미지 받기 + 실행

기본값은 GHCR에 미리 빌드된 `ghcr.io/saintsc-ai/myphotos:latest`를 pull하므로
NAS에서 빌드할 필요가 없습니다. 먼저 이미지를 받고:

```bash
sudo docker compose pull
```

받은 뒤 컨테이너를 띄웁니다:

```bash
sudo docker compose up -d
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

## 3) 로그 / 상태

```bash
docker compose ps
docker compose logs -f api worker
docker compose logs -f ml-worker          # ml profile 켰을 때
```

## 4) 업데이트

main에 새 커밋이 푸시되면 GHCR의 `latest` 태그가 갱신됩니다. NAS에서는:

```bash
docker compose pull
docker compose up -d                      # 변경된 컨테이너만 재기동
```

`git pull`은 docker-compose.yml/.env 같은 호스트 파일이 바뀌었을 때만
필요합니다. `alembic upgrade head`는 api 컨테이너 시작 시 자동 실행되므로
별도 수동 마이그레이션 불필요. 워커들은 api 컨테이너가 healthy(=마이그레이션
완료)될 때까지 기다렸다가 시작합니다.

## 5) 다른 호스트로 이전

- `DATA_DIR` 경로 통째로 + `config/local.toml`만 새 호스트에 옮기고
  같은 절차를 반복하면 됩니다 (재인덱싱 없음).
- DSM ↔ Linux ↔ Windows 호스트 간 이전도 동일. `roots.abs_path`만 새
  호스트의 컨테이너 내부 경로 (`/photos`)에 맞게 관리 UI에서 한 번 갱신.

## 동작 메모

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

## Docker 트러블슈팅

| 증상 | 원인 / 해결 |
| --- | --- |
| 관리 UI에 root 추가했더니 상태가 **`접근 불가`** | 거의 항상 둘 중 하나입니다. ① 경로를 컨테이너 안 경로가 아니라 호스트 경로(`/volume1/photo`)로 입력 → `/photos`로 수정. ② 폴더가 UID 1000에게 안 읽힘. 공유 폴더(`/volume1/photo`)는 보통 `0777`이라 문제없지만, 개인 공간(`/volume1/homes/.../Photos`)은 `d---------+`(ACL 전용)이라 못 읽음 → 호스트에서 `sudo chmod -R a+rX <경로>` (그래도 안 되면 `chmod -R 777` 또는 DSM ACL). |
| 컨테이너에서 사진이 진짜 보이는지 빠르게 확인 | `docker compose exec api ls /photos \| head` — 파일이 보여야 정상. `Permission denied`면 위 ② 권한 문제. |
| `docker: 'compose' is not a docker command` | DSM Container Manager에 v2 plugin이 등록 안 된 상태. 위 "DSM에서 docker CLI가 안 잡힐 때" 섹션의 plugin 등록 또는 `docker-compose` (하이픈) 사용. |
| `PermissionError: [Errno 13] Permission denied` (`/var/run/docker.sock`) | SSH 사용자가 docker group 멤버가 아닙니다. 빠른 해결은 `sudo` 붙여 호출. 영구 해결은 `sudo synogroup --add docker $USER` 후 SSH 재접속. 위 "Docker 소켓 권한" 섹션 참고. |
| `Bind mount failed: '...' does not exists` | `.env`의 `DATA_DIR` (또는 `CONFIG_DIR`) 경로가 호스트에 없어서. 바인드 마운트는 자동 생성 안 됨. `mkdir -p /path/to/data` 한 번 만들어 두고 다시 `up -d`. |
| `Container "..." is unhealthy` → worker/ml-worker가 안 뜸 | api 컨테이너가 부팅 중 죽은 것 (worker는 `depends_on: api: service_healthy`라 api가 healthy일 때만 시작). **`docker compose logs api`** (또는 `docker logs <id>`)를 먼저 보세요. `Permission denied`가 보이면 마운트한 호스트 경로가 컨테이너 UID(1000) 소유가 아니라서입니다 — `/app/data/...`면 `DATA_DIR`(entrypoint의 `mkdir`/DB 쓰기 실패), `/app/config/default.toml`이면 `config/`(alembic의 설정 읽기 실패, crash 루프). 둘 다 한 번에: `sudo chown -R 1000:1000 "$(grep -E '^DATA_DIR=' .env \| cut -d= -f2-)" config` 후 `docker compose up -d`. |
| `Bind for 0.0.0.0:8888 failed: port is already allocated` | 이전에 띄운 MyPhotos 컨테이너가 같은 포트를 잡고 있는 경우가 대부분. `docker ps --format '{{.Names}}\t{{.Ports}}' \| grep 8888`으로 찾고, `docker ps -aq --filter 'name=myphotos' \| xargs -r docker rm -f` 또는 이전 폴더에서 `docker compose down`. 그 외 다른 서비스가 점유했다면 `.env`의 `API_PORT`를 9888 등으로 변경. |
| `git clone .` 실행 시 `destination path '.' already exists` | 폴더에 뭔가 남아있는 상태. 깨끗하게 다시 받기: `cd .. && rm -rf myphotos && mkdir myphotos && cd myphotos && git clone https://github.com/saintsc-ai/MyPhotos.git .` (DATA_DIR이 같은 폴더 안의 `data/`였다면 미리 옮겨두기) |
| 스캔/색인이 멈춰 보이고 잡 큐가 계속 쌓임 | 이전에 잘못된 경로·권한으로 등록된 잡들이 큐를 막고 있는 경우가 많습니다. 관리 → **색인** 탭 → **잡 큐** 섹션의 "대기·실패 잡 비우기" 또는 "실행 중 포함 전체 비우기" 버튼으로 정리한 뒤 다시 스캔. CLI로도 가능: `curl -X POST http://NAS:8888/api/admin/jobs/purge -H "Content-Type: application/json" -d '{"include_running":true}'` |
| Synology Photos가 같은 폴더에 쓰는 중인데 충돌이 걱정 | `read-only` 옵션을 켠 상태(권장)면 MyPhotos는 원본을 절대 수정하지 않습니다. ACL 권한만 풀어주면 됨. |
| UID/GID를 바꿨는데 반영 안 됨 | `APP_UID/GID`는 빌드 인자라서 `docker compose up -d --build` 로 이미지를 다시 빌드해야 적용됩니다 (GHCR 이미지를 쓸 땐 변경 효과가 제한적이라 호스트 폴더에 `chmod` 쪽이 더 단순). |
| 새 컨테이너가 빈 DB로 시작 (이전 데이터 안 보임) | 이전 컨테이너의 `DATA_DIR`을 새 `.env`가 다른 위치로 가리키고 있는 경우. `docker inspect 이전컨테이너 --format '{{range .Mounts}}{{.Source}} → {{.Destination}}\n{{end}}'`로 옛 위치 확인 → 새 `.env`의 `DATA_DIR`을 그쪽으로 바꾸거나 데이터를 새 위치로 옮기기. |

## 컨테이너 안 vs 호스트 명령 — 어디서 무엇을 실행?

| 명령 종류 | 실행 위치 | 예 |
| --- | --- | --- |
| `docker compose ...` | **호스트** | `docker compose up -d`, `docker compose logs -f worker` |
| 호스트 파일 권한·경로 조작 | **호스트** | `chmod 777 /volume1/photo` (컨테이너 안에선 `:ro`라 의미 없음) |
| HTTP 호출 (`curl /api/...`) | **어디서든** (NAS에 닿기만 하면) | `curl http://NAS:8888/healthz` |
| 컨테이너 내부 확인 / 디버깅 | **컨테이너 안 한 줄** | `docker compose exec api ls /photos`, `docker compose exec api bash` |
| `alembic upgrade head` | **자동** (entrypoint가 시작 시 실행) | 수동 호출 불필요 |

운영 명령은 거의 다 호스트에서. 컨테이너 안 쉘은 디버깅용으로만.

## `git pull` vs `docker compose pull` — 언제 무엇이 필요?

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

---

# English

## Docker install guide

> [← Back to README](../../README.md)

Skip the Python / uv / exiftool / ffmpeg install on the host and run
everything as containers. One image, three containers (API + indexing
worker + optional ML worker). Same flow on DSM Container Manager, a
regular Linux + Docker, or Windows + Docker Desktop.

### 0) Prerequisites

- Docker 20.10+ / Docker Compose v2 (DSM 7.2+ ships both in the
  "Container Manager" package)
- **Git** — install "Git Server" from the DSM Package Center (needed
  for `git clone`). Verify with `git --version`. The Synology guide's
  [Prerequisites](synology.md#english) box has the full DSM-specific
  walkthrough.
- Host path to the photo library — e.g. `/volume1/photo`
- Host path for runtime data — e.g. `/volume1/docker/myphotos/data`.
  **Create it *and* hand it to the container's UID ahead of time**
  (exact commands in [step 1](#1-clone--create-the-env-file)). Docker
  bind mounts don't auto-create the host path (`compose up` fails with
  `Bind mount failed: ... does not exists`), and even when the path does
  exist it's typically **root-owned** — the container runs as a non-root
  user (`APP_UID`, default 1000), so it can't write `/app/data` and the
  api container dies on boot (→ worker reports `Container "..." is
  unhealthy`).

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
# A. Use v1 as-is — substitute every `docker compose` in this guide
#    with `docker-compose`. Functionally identical.
docker-compose --version

# B. Register the v2 plugin once
mkdir -p ~/.docker/cli-plugins
ln -sf /var/packages/ContainerManager/target/usr/libexec/docker/cli-plugins/docker-compose \
       ~/.docker/cli-plugins/docker-compose
docker compose version
```

> ⚠ The rest of the guide uses `docker compose` (v2 spacing). If
> you went with v1, mentally hyphenate every such command.

#### Docker socket permission (Synology / Linux SSH user)

If your SSH user isn't in the `docker` group, every `docker` /
`docker-compose` call fails with `PermissionError: [Errno 13] Permission
denied` (the client can't open `/var/run/docker.sock`). Pick one:

**A. Prefix every call with `sudo` — works immediately**

```bash
sudo docker compose pull
sudo docker compose up -d
```

Read every `docker compose ...` in this guide as `sudo docker compose ...`.

**B. Add yourself to the docker group — one-time**

```bash
sudo synogroup --add docker $USER
```

DSM uses `synogroup` instead of the standard `usermod -aG docker $USER`.
Log out of SSH and reconnect for the new group to apply — then `sudo`
is no longer needed.

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
- `APP_UID / APP_GID` — **build-time args**, so they don't apply to the
  prebuilt GHCR image (the default). Leave them at 1000 and fix host perms
  per the ⚠ box below (chown `DATA_DIR` + `config/`, plus photo-folder
  read). Only set them to the photo-owning account's `id -u` / `id -g`
  when you build your own image to run as a different UID.

> ⚠ The container runs as a non-root user (UID **1000**), so **both host
> paths it mounts** must be owned by that UID — `DATA_DIR` (DB / thumbs
> **write**) and `config/` (read `default.toml` + write `local.toml`).
> Otherwise the entrypoint's `mkdir -p /app/data/logs …` *or* alembic
> reading `config/default.toml` fails with `Permission denied`, the api
> never goes healthy, and the worker errors out with `Container "…" is
> unhealthy`. Right after writing `.env` (required):
>
> ```bash
> DATA_DIR=$(grep -E '^DATA_DIR=' .env | cut -d= -f2-)
> CONFIG_DIR=$(grep -E '^CONFIG_DIR=' .env | cut -d= -f2-)
> mkdir -p "$DATA_DIR"
> sudo chown -R 1000:1000 "$DATA_DIR" "${CONFIG_DIR:-./config}"
> ```
>
> `config/` lives inside the git checkout, so if a later `git pull` needs
> to update files there and hits a permission clash, run `sudo git pull`
> or re-run the chown (to avoid touching git ownership, use
> `sudo chmod -R a+rwX "${CONFIG_DIR:-./config}"` instead).
>
> The photo folder (`PHOTO_ROOT`) is mounted read-only, but UID 1000 still
> has to be able to **read** it. With Synology Photos:
>
> - **Shared space** (`/volume1/photo`, shared albums) is a shared folder,
>   so it's usually `drwxrwxrwx+ root root` (0777) and **needs no perm
>   changes**.
> - **Personal space** (`/volume1/homes/<user>/Photos`) is locked per-user
>   (`d---------+`, ACL-only), so UID 1000 can't read it.
>
> Verify before registering with `sudo docker-compose exec api ls /photos`;
> only if files don't show or a root reports `접근 불가` (no access) do you
> need to open it up:
>
> ```bash
> PHOTO_ROOT=$(grep -E '^PHOTO_ROOT=' .env | cut -d= -f2-)
> sudo chmod -R a+rX "$PHOTO_ROOT"   # still locked? chmod -R 777, or an ACL in DSM
> ```
>
> Synology metadata dirs (`@eaDir`, `#recycle`, …) are skipped by the
> scanner by default — nothing to configure.
>
> The GHCR image is baked with UID/GID **1000** — leave `APP_UID/GID` at
> the default and just make `DATA_DIR` owned by 1000. Changing them in
> `.env` has no effect while you run the prebuilt image (the runtime UID
> stays 1000); running as a different UID needs a local build — see the
> "Changed `APP_UID/GID` but it didn't take effect" troubleshooting row.

### 2) Pull the image + start

The default image is `ghcr.io/saintsc-ai/myphotos:latest`, prebuilt by GitHub
Actions — no local build needed on the NAS. Pull the image first:

```bash
sudo docker compose pull
```

Then bring the containers up:

```bash
sudo docker compose up -d
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
| Root row shows **`접근 불가` (no access)** | Almost always one of: ① the path was entered as a host path (`/volume1/photo`) instead of the in-container path → edit to `/photos`. ② The folder isn't readable by UID 1000. A shared folder (`/volume1/photo`) is usually `0777` and fine; a personal space (`/volume1/homes/.../Photos`) is `d---------+` (ACL-only) and isn't → `sudo chmod -R a+rX <path>` on the host (or `chmod -R 777` / a DSM ACL if that's not enough). |
| Quick sanity check from outside the UI | `docker compose exec api ls /photos \| head` — files visible = OK. `Permission denied` means the ACL issue above. |
| `docker: 'compose' is not a docker command` | Container Manager didn't register the v2 plugin. See the "When the docker CLI isn't on PATH" subsection above for plugin registration, or just use `docker-compose` (hyphenated). |
| `PermissionError: [Errno 13] Permission denied` (`/var/run/docker.sock`) | SSH user isn't in the `docker` group. Quick fix: prefix the call with `sudo`. Permanent fix: `sudo synogroup --add docker $USER`, then reconnect SSH. See the "Docker socket permission" subsection above. |
| `Bind mount failed: '...' does not exists` | The `DATA_DIR` (or `CONFIG_DIR`) path in `.env` doesn't exist on the host. Bind mounts don't auto-create — `mkdir -p /path/to/data` once and re-run `up -d`. |
| `Container "..." is unhealthy` → worker/ml-worker won't start | The api container died during boot (the worker has `depends_on: api: service_healthy`, so it only starts once api is healthy). Check **`docker compose logs api`** (or `docker logs <id>`) first. A `Permission denied` means a mounted host path isn't owned by the container UID (1000): `/app/data/...` → `DATA_DIR` (entrypoint `mkdir` / SQLite write fails); `/app/config/default.toml` → `config/` (alembic can't read settings, crash-loops). Fix both at once: `sudo chown -R 1000:1000 "$(grep -E '^DATA_DIR=' .env \| cut -d= -f2-)" config`, then `docker compose up -d`. |
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

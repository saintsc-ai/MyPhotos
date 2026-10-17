# Docker + NVIDIA GPU 설치 가이드

> 한국어 / [English](#english)

> [← Docker 가이드](docker.md) · [← README로 돌아가기](../../README.md)

ML 분석(YOLO 객체 · CLIP 임베딩 · 얼굴 검출/임베딩 · OCR)을 NVIDIA GPU로
돌리는 Docker 배포입니다. API · 인덱싱 워커 · ML 워커 · DB(SQLite) ·
썸네일을 **GPU가 있는 Docker 호스트 한 대**에 두고, 사진 원본은 NAS에서
NFS(또는 SMB)로 붙이는 형태를 기준으로 합니다.

기본 [Docker 가이드](docker.md)의 CPU 이미지와 같은 코드·같은 compose를
쓰고, GPU용 이미지 타깃과 compose overlay만 추가됩니다.

## 0) 호스트 준비

| 항목 | 요구 사항 |
|---|---|
| GPU | NVIDIA, compute capability 6.0+ (Pascal 이상). VRAM 4GB+ 권장 |
| 드라이버 | **550 이상** (CUDA 12.4). 더 새 드라이버도 동작 |
| Docker | Docker Engine 24+ / Compose v2 |
| NVIDIA Container Toolkit | 설치 + `nvidia-ctk runtime configure --runtime=docker` |
| RAM | 8GB 이상 권장 (아래 "자원 사용량" 참고) |

1. NVIDIA 드라이버 설치 (배포판 패키지 또는 NVIDIA 공식 저장소). `nvidia-smi`가
   GPU와 드라이버 버전을 보여주면 OK.
2. [NVIDIA Container Toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html) 설치 후:
   ```bash
   sudo nvidia-ctk runtime configure --runtime=docker
   sudo systemctl restart docker
   ```
3. 컨테이너에서 GPU가 보이는지 확인:
   ```bash
   docker run --rm --gpus all nvidia/cuda:12.4.1-base-ubuntu22.04 nvidia-smi
   ```

> 이미지 안의 CUDA 런타임은 **12.4**(pip 휠: cuDNN 9.1 · onnxruntime-gpu 1.20.2)
> 입니다. 호스트에는 CUDA Toolkit을 설치할 필요가 없고 **드라이버만** 있으면
> 됩니다. 드라이버가 550보다 오래됐다면 드라이버부터 올리세요.

## 1) 사진 원본 연결 (NFS 권장)

앱은 사진을 **삭제(휴지통으로 이동) · 이동 · 회전**하므로 원본은
**읽기/쓰기**로 붙입니다. 원본을 보호하고 싶은 라이브러리는 관리 화면의
루트별 "읽기 전용" 토글로 막습니다.

**NFS (권장)** — 호스트에서 마운트하고 그 경로를 `PHOTO_ROOT`로 넘깁니다.

```bash
sudo apt install nfs-common
sudo mkdir -p /mnt/photos
# /etc/fstab
<nas-host>:/path/to/photos  /mnt/photos  nfs  rw,hard,nfsvers=4.1,noatime,_netdev  0  0
sudo mount -a
```

- NAS 쪽 NFS 권한: Docker 호스트 IP에 읽기/쓰기 허용. squash는 "No mapping"
  (UID/GID를 그대로 전달)이어야 아래 `PUID/PGID/PGIDS`가 의미가 있습니다.
- NFS는 클라이언트가 보낸 UID/GID로 권한을 판정합니다 → 컨테이너가 쓰는
  UID/GID(아래 2단계)를 NAS 쪽 소유자/그룹과 맞추세요.

**SMB** — `mount -t cifs ... ,uid=<PUID>,gid=<PGID>` 로 호스트에 마운트하거나
compose의 cifs 볼륨을 씁니다. SMB는 파일 소유권을 마운트 옵션 하나로
덮어쓰므로 보조 그룹 기반 권한이 그대로 드러나지 않고, 대량 썸네일/스캔에서
NFS보다 느린 경우가 많습니다.

**여러 라이브러리** — `PHOTO_ROOT`(→ `/photos`) 외에 `PHOTO_ROOT_2`,
`PHOTO_ROOT_3`, `PHOTO_ROOT_4`(→ `/photos2`…`/photos4`)를 쓸 수 있습니다.
비워 두면 빈 폴더가 붙습니다. 관리 화면에서 루트를 추가할 때는 컨테이너
안 경로(`/photos2/...`)를 입력합니다. 넷보다 많으면 공통 상위 폴더를
`PHOTO_ROOT`로 붙이고 그 아래 하위 폴더를 루트로 등록하세요(하위 NFS
마운트도 함께 보입니다).

## 2) 실행 사용자 (PUID / PGID / 보조 그룹)

GPU overlay는 컨테이너를 root로 시작한 뒤 entrypoint가 곧바로 지정한
사용자로 내려갑니다(`setpriv`). 앱 프로세스는 root로 돌지 않습니다.

| `.env` | 의미 |
|---|---|
| `PUID` | 실행 UID (NAS에서 사진 소유자 UID) |
| `PGID` | 주 그룹 GID |
| `PGIDS` | **보조 그룹** GID 목록, 쉼표 구분 (예: `100,1001`) |
| `UMASK` | 새 파일 권한 마스크 (예: `0002` → 그룹 쓰기 유지) |

NAS 폴더 권한이 "주 그룹 + 보조 그룹"으로 걸려 있으면 `PGIDS`가 빠졌을 때
일부 폴더가 `Permission denied`로 거부됩니다. NAS에서 `id <user>`로 나오는
`groups=`의 GID를 모두 넣으세요. 시작 로그 첫 줄에
`==> running as uid=… gid=… groups=…`가 찍힙니다.

`DATA_DIR`(DB·썸네일)과 `CONFIG_DIR`은 `PUID`가 읽고 쓸 수 있어야 합니다:
`sudo chown -R <PUID>:<PGID> /path/to/data /path/to/config`.

## 3) 휴지통 위치

사진 삭제는 파일을 휴지통 폴더로 **이동**합니다. 휴지통이 사진과 **다른
마운트**(예: 사진은 NFS, `data/`는 로컬 디스크)에 있으면 이동이
"복사 후 삭제"가 되어 큰 동영상 삭제가 느리고, 그동안 양쪽 공간을 씁니다.
Linux에서는 같은 파일시스템이라도 **마운트 지점이 다르면** rename이 안 됩니다.

해결: 휴지통을 사진과 **같은 마운트 안**, 스캐너가 무시하는 이름 아래에 둡니다.

```ini
# .env
TRASH_DIR_IN_CONTAINER=/photos/.trash/myphotos
```

- `.trash`는 기본 `[scanner] ignore_dirs`에 있어 색인되지 않습니다.
- 휴지통은 **한 곳**입니다. 라이브러리가 여러 마운트에 걸쳐 있으면 같은
  마운트의 사진만 빠르게 이동되고 나머지는 복사 후 삭제가 됩니다.
- 휴지통 위치를 바꿀 때는 **기존 휴지통 내용도 새 위치로 옮기세요**
  (복원은 `<휴지통>/<photo_id>/`를 찾습니다).
- 휴지통 디스크 여유 검사는 휴지통이 있는 볼륨 기준입니다.

## 4) 빌드 · 실행

```bash
git clone https://github.com/saintsc-ai/MyPhotos.git myphotos && cd myphotos
cp .env.example .env     # PHOTO_ROOT, DATA_DIR, CONFIG_DIR, API_PORT, PUID/PGID/PGIDS …

C="docker compose -f docker-compose.yml -f docker-compose.gpu.yml"
$C build                       # myphotos:gpu (약 2.1GB 압축 / 6.4GB 디스크)
$C --profile ml up -d
$C exec ml-worker ./scripts/install-ml-models.sh     # 최초 1회, data/models (~200MB)
$C restart ml-worker
```

GPU 관련 `.env` 키:

| 키 | 기본값 | 설명 |
|---|---|---|
| `GPU_IMAGE` | `myphotos:gpu` | 빌드/실행 이미지 태그 |
| `GPU_DEVICE_ID` | `0` | ML 워커에 줄 GPU 번호 |
| `CUDA_MEM_LIMIT_MB` | `0`(제한 없음) | ONNX 세션 **하나당** CUDA 메모리 상한 |
| `ONNX_PROVIDERS` | `CUDAExecutionProvider,CPUExecutionProvider` | `CPUExecutionProvider`로 두면 GPU 없이 실행 |

GPU는 ML 워커에만 할당됩니다. API·인덱싱 워커는 같은 이미지를 CPU로 씁니다.

**GPU가 실제로 잡혔는지 확인**:

```bash
$C logs ml-worker | grep -E "ONNX session|OCR backend|CUDA requested"
# ONNX session yolov8n.onnx providers=['CUDAExecutionProvider', 'CPUExecutionProvider']
# OCR backend: rapidocr v3 (lang=korean, device=cuda)
```

GPU를 못 쓰면 CPU로 계속 돌고 `CUDA requested but not attached` 경고가 남습니다
(드라이버 / `--gpus` / Container Toolkit 확인).

`config/local.toml`로도 조정할 수 있습니다(환경변수가 있으면 환경변수 우선):

```toml
[ml]
onnx_cuda_device_id = 0
onnx_cuda_mem_limit_mb = 0          # 세션당 상한, 0 = 없음
onnx_cudnn_conv_algo_search = "HEURISTIC"

[ocr]
use_gpu = true                      # false → OCR만 CPU
```

### 4-1) GPU 이미지 직접 빌드하기

GHCR에 자동으로 올라가는 이미지는 **CPU용뿐**이라, GPU 이미지는 직접 빌드합니다.
위 `$C build`가 그 빌드입니다. 아래는 빌드를 따로 다룰 때의 상세입니다.

**준비물**

- x86_64(amd64) Linux + Docker. **빌드 자체에는 GPU가 필요 없습니다.**
  실행할 호스트와 다른 PC에서 빌드해도 됩니다(아래 "다른 PC에서 빌드해 옮기기").
- 디스크 여유 **약 25GB**(완성 이미지 6.4GB + 빌드 캐시. 빌드 후 정리 가능).
- 인터넷: PyPI(CUDA/cuDNN 휠 약 2GB)와 OCR 모델 다운로드 서버(modelscope.cn)에 접속할 수 있어야 합니다.
- 시간: 8코어 기준 약 10분(대부분 이미지 레이어 내보내기).

**빌드**

```bash
# compose로 (권장 — .env의 GPU_IMAGE 태그로 만듦)
docker compose -f docker-compose.yml -f docker-compose.gpu.yml build

# 또는 docker 직접
docker build --target gpu -t myphotos:gpu .
```

`--target gpu`를 빼면 CPU 이미지가 만들어집니다. 실행 UID는 `PUID/PGID`로
**실행할 때** 정하므로, UID 때문에 다시 빌드할 필요는 없습니다.

**빌드 확인** (GPU 호스트에서):

```bash
docker run --rm --gpus all --entrypoint python myphotos:gpu \
  -c "import onnxruntime as o; print(o.__version__, o.get_device(), o.get_available_providers())"
# 1.20.2 GPU [..., 'CUDAExecutionProvider', 'CPUExecutionProvider']
```

**업데이트 (`git pull` 후)** — 코드가 이미지 안에 들어가므로 매번 다시 빌드합니다.

```bash
git pull
$C build
$C --profile ml up -d          # 바뀐 이미지로 컨테이너 재생성
docker image prune -f          # 이전 이미지 정리
docker builder prune -f        # 빌드 캐시 정리 (수 GB~20GB 회수)
```

**다른 PC에서 빌드해 옮기기** (GPU 호스트에 빌드할 디스크·네트워크가 부족할 때):

```bash
# 빌드 PC
docker build --target gpu -t myphotos:gpu .
docker save myphotos:gpu | gzip > myphotos-gpu.tar.gz      # 약 2.1GB
# GPU 호스트로 복사한 뒤
gunzip -c myphotos-gpu.tar.gz | docker load
```

GPU 호스트에서는 `$C build` 없이 `$C --profile ml up -d`만 실행합니다
(같은 `GPU_IMAGE` 태그를 씀).

**버전 조정** — 기본 구성은 onnxruntime-gpu 1.20.2 + CUDA 12.4 + cuDNN 9.1이고
드라이버 550 이상에서 돕니다. 드라이버가 더 새것이면 그대로 쓰면 됩니다.
onnxruntime 버전만 바꿀 때:

```bash
docker build --target gpu --build-arg ORT_GPU_VERSION=1.20.2 -t myphotos:gpu .
```

CUDA 계열을 바꾸려면 Dockerfile의 `nvidia-*` 고정 버전을 onnxruntime-gpu
[호환표](https://onnxruntime.ai/docs/execution-providers/CUDA-ExecutionProvider.html)에
맞춰 함께 올려야 합니다(드라이버도 그 CUDA를 지원해야 함).

**빌드 문제 해결**

| 증상 | 원인 / 조치 |
|---|---|
| `No matching distribution found for onnxruntime-gpu==…` | PyPI에 없는 버전 → `pip index versions onnxruntime-gpu`로 있는 버전 확인 후 `ORT_GPU_VERSION` 지정 |
| OCR 모델 단계(`_build_v3` / `grep -q korean_PP-OCRv4`)에서 실패 | modelscope.cn 접속 차단 → 방화벽/프록시 확인. 빌드 시 프록시는 `--build-arg HTTPS_PROXY=…` |
| `no space left on device` | 빌드 캐시 → `docker builder prune -f`, 여유 25GB 확보 |
| 빌드는 됐는데 실행 시 `CUDA requested but not attached` | 빌드 문제가 아니라 호스트 문제 → 0) 호스트 준비 확인 |

## 5) 자원 사용량 · 속도

측정 환경: Turing 계열 8GB GPU · 8 vCPU · RAM 8GB VM, 공개 데이터셋 사진을
1024px 썸네일로 만든 50장(장당 평균 ms, 한 스레드에서 순차 실행).

| 모델 | GPU (CUDA) | CPU 1스레드 (기본값) | CPU 8스레드 |
|---|---|---|---|
| YOLO 객체 | **20** | 120 | 79 |
| CLIP 임베딩 (int8) | 41 | 43 | **27** |
| 얼굴 검출+임베딩 | **41** | 73 | 84 |
| OCR (검출+인식) | **105** | 353 | 346 |
| 합계 | **≈ 210** | ≈ 590 | ≈ 535 |

- 전체 파이프라인(ML 워커 기본 2스레드)에서 사진 300장의 ML 4종이 약 50–60초
  — **시간당 약 2만 장**. CPU만 쓰면 약 3배 느립니다.
- **CLIP은 GPU 이득이 없습니다.** 배포 모델이 int8 양자화본이라 일부 연산이
  CPU로 돌고 GPU↔CPU 복사가 생깁니다(로그의 `Memcpy nodes are added` 경고).
  나머지 세 모델이 GPU 이득의 대부분입니다.
- OCR(rapidocr)은 CPU에서 자체적으로 모든 코어를 쓰므로 스레드 설정과 무관합니다.
- GPU 사용률은 20% 안팎이고 병목은 CPU 쪽 전·후처리입니다. ML 워커 스레드를
  4로 올리면(`[worker] ml_concurrency = 4`) 처리량이 약 15–20% 늘지만 VRAM·RAM도 늘어납니다.

**메모리**

| | ML 2스레드 (기본) | ML 4스레드 |
|---|---|---|
| VRAM (모델 전부 로드, 처리 중) | ≈ 2.3GB | ≈ 3.7GB |
| ML 워커 RAM | ≈ 1.2GB | ≈ 1.5GB |
| API / 인덱싱 워커 RAM | ≈ 0.2GB / 0.15GB (대기 중) | |

- 단일 스레드로 모든 모델을 올리면 VRAM ≈ 0.9–1.2GB입니다.
- `CUDA_MEM_LIMIT_MB`는 **세션당** 상한입니다. **512 이상**을 쓰세요. 256에서는 OCR
  검출이 큰 이미지에서 할당에 실패합니다. 512 상한에서 속도 차이는 거의 없었습니다.
- 디스크: 이미지 약 6.4GB, `data/` = 썸네일(사진당 약 0.1–0.3MB, 256px+1024px)
  + `catalog.db`(FTS·임베딩·작업 기록 포함이라 라이브러리에 따라 수 GB 이상)
  + ML 모델 약 200MB. 기존 설치가 있으면 그 `data/` 크기(`du -sh data/*`)에
  여유 50%를 더해 잡는 것이 가장 정확합니다. DB는 로컬 SSD에 두세요.

## 6) 기존 설치에서 옮길 때

DB에서 호스트마다 다른 값은 **`roots.abs_path` 하나뿐**입니다
(`photos.rel_path`는 루트 기준 상대 경로, 썸네일은 SHA-256 주소).
순서:

1. 기존 서비스 정지 → DB 스냅샷 (`sqlite3 catalog.db ".backup …"`) —
   [porting.md](../operations/porting.md#1-원본-호스트--정합성-있는-스냅샷).
2. `data/` 전체(catalog.db · thumbs · proxies · trash · models · session.secret)와
   `config/local.toml`을 새 호스트의 `DATA_DIR` / `CONFIG_DIR`로 복사.
   소유자를 `PUID:PGID`로.
3. 사진 원본을 같은 내용으로 마운트(1단계).
4. **컨테이너를 올리기 전에** 루트 경로를 컨테이너 안 경로로 재작성:
   ```bash
   $C run --rm --no-deps api python -m app.tools.cutover --list
   $C run --rm --no-deps api python -m app.tools.cutover --map "/old/abs/path=/photos" --apply
   ```
   경로가 틀린 채로 워커가 스캔하면 모든 사진이 "missing"으로 표시됩니다
   (경로를 고치고 다시 스캔하면 돌아오지만, 처음부터 맞추는 편이 안전).
5. `$C --profile ml up -d` — api가 `alembic upgrade head`를 실행합니다.
6. 휴지통을 옮긴다면(3단계) 기존 `data/trash/` 내용을 새 위치로 이동.

주의:
- SQLite는 **로컬 디스크**에 두세요. DB를 NFS/SMB 위에 두면 잠금이 불안정합니다.
- 옛 설치와 새 설치를 **동시에** 같은 원본에 대해 돌리지 마세요(이중 스캔·이동).
- `config/local.toml`의 `[ml] onnx_providers`는 GPU 이미지에선 환경변수
  `MYPHOTOS_ONNX_PROVIDERS`가 우선합니다.

## 문제 해결

| 증상 | 확인 |
|---|---|
| `could not select device driver "nvidia"` | Container Toolkit 미설치 / `nvidia-ctk runtime configure` 후 docker 재시작 안 함 |
| 로그에 `CUDA requested but not attached` | `nvidia-smi` 드라이버 ≥ 550? `docker run --gpus all … nvidia-smi` 동작? |
| `CUDA_ERROR_OUT_OF_MEMORY` / 다른 컨테이너와 VRAM 다툼 | `CUDA_MEM_LIMIT_MB` 설정 (세션 수 × 상한 + 컨텍스트 ≈ 사용량) |
| 일부 폴더 `Permission denied` | `PGIDS`에 NAS 보조 그룹 GID 누락 / NFS squash 설정 |
| OCR `Unsupported rec.lang_type='korean'` | `[ocr] ocr_version`을 비우지 마세요(기본 `PP-OCRv4`) |
| 삭제가 느림 / 휴지통 용량 부족 | 휴지통이 다른 마운트 → 3단계 |

---

# English

## Docker + NVIDIA GPU install guide

Runs ML analysis (YOLO objects · CLIP embeddings · face detection/embedding ·
OCR) on an NVIDIA GPU. The reference layout keeps the API, indexing worker,
ML worker, SQLite DB and thumbnails on **one Docker host with a GPU**, with the
photo originals attached from a NAS over NFS (or SMB). Same code and base
compose as the CPU [Docker guide](docker.md) — only a GPU image target and a
compose overlay are added.

### 0) Host preparation

| Item | Requirement |
|---|---|
| GPU | NVIDIA, compute capability 6.0+ (Pascal or newer), 4GB+ VRAM recommended |
| Driver | **550 or newer** (CUDA 12.4); newer drivers work too |
| Docker | Engine 24+ / Compose v2 |
| NVIDIA Container Toolkit | installed + `nvidia-ctk runtime configure --runtime=docker` |
| RAM | 8GB+ recommended |

```bash
sudo nvidia-ctk runtime configure --runtime=docker && sudo systemctl restart docker
docker run --rm --gpus all nvidia/cuda:12.4.1-base-ubuntu22.04 nvidia-smi
```

The image ships its own CUDA 12.4 runtime + cuDNN 9.1 + onnxruntime-gpu 1.20.2
(pip wheels) — the host needs **only the driver**, no CUDA toolkit.

### 1) Attach the photo originals (NFS recommended)

The app deletes (to trash), moves and rotates photos, so mount originals
**read-write**; protect individual libraries with the per-root read-only
toggle in the admin UI.

```bash
# /etc/fstab
<nas-host>:/path/to/photos  /mnt/photos  nfs  rw,hard,nfsvers=4.1,noatime,_netdev  0  0
```

On the NAS allow read/write for the Docker host and use "no mapping" squash so
the UID/GIDs below reach the NAS unchanged. SMB works too (`mount -t cifs …`)
but maps ownership through mount options, so group-based permissions don't
carry over, and it is usually slower for bulk scans.

Several libraries: besides `PHOTO_ROOT` (→ `/photos`) use `PHOTO_ROOT_2..4`
(→ `/photos2`…`/photos4`); unset slots mount an empty dir. Register roots with
the in-container path. For more, mount a common parent as `PHOTO_ROOT`.

### 2) Runtime user (PUID / PGID / supplementary groups)

The overlay starts containers as root; the entrypoint immediately drops to the
given identity with `setpriv` — no app process runs as root.

| `.env` | Meaning |
|---|---|
| `PUID` | UID to run as (owner of the photos on the NAS) |
| `PGID` | primary GID |
| `PGIDS` | **supplementary** GIDs, comma-separated (e.g. `100,1001`) |
| `UMASK` | e.g. `0002` to keep new files group-writable |

If NAS folders grant access through a secondary group, leaving `PGIDS` out
makes those folders fail with `Permission denied`. Use every GID from `id <user>`
on the NAS. The first log line shows `==> running as uid=… gid=… groups=…`.
`DATA_DIR` and `CONFIG_DIR` must be writable by `PUID`.

### 3) Trash location

Deleting a photo **moves** it into the trash dir. If the trash is on a
**different mount** than the photo (photos on NFS, `data/` on local disk), the
move becomes copy + delete — slow for large videos and temporarily using space
on both sides. On Linux even the same filesystem behind two mount points can't
be renamed across. Put the trash **inside the photo mount**, under a
scanner-ignored name:

```ini
TRASH_DIR_IN_CONTAINER=/photos/.trash/myphotos
```

`.trash` is in the default `[scanner] ignore_dirs`. There is one trash dir, so
photos on other mounts still copy + delete. When changing the location, move
the existing trash contents too (restore looks up `<trash>/<photo_id>/`).

### 4) Build · run

```bash
cp .env.example .env     # PHOTO_ROOT, DATA_DIR, CONFIG_DIR, API_PORT, PUID/PGID/PGIDS …
C="docker compose -f docker-compose.yml -f docker-compose.gpu.yml"
$C build                       # myphotos:gpu (~2.1GB compressed / 6.4GB on disk)
$C --profile ml up -d
$C exec ml-worker ./scripts/install-ml-models.sh     # once, data/models (~200MB)
$C restart ml-worker
```

| Key | Default | Meaning |
|---|---|---|
| `GPU_IMAGE` | `myphotos:gpu` | image tag |
| `GPU_DEVICE_ID` | `0` | GPU given to the ML worker |
| `CUDA_MEM_LIMIT_MB` | `0` (off) | CUDA memory cap **per ONNX session** |
| `ONNX_PROVIDERS` | `CUDAExecutionProvider,CPUExecutionProvider` | `CPUExecutionProvider` = no GPU |

Only the ML worker gets the GPU; the API and indexing worker run the same image
on CPU. Verify with
`$C logs ml-worker | grep -E "ONNX session|OCR backend|CUDA requested"` — sessions
should list `CUDAExecutionProvider` first and OCR should say `device=cuda`. If
the GPU is unusable the worker keeps running on CPU and logs
`CUDA requested but not attached`. The same knobs exist in `config/local.toml`
(`[ml] onnx_cuda_device_id / onnx_cuda_mem_limit_mb / onnx_cudnn_conv_algo_search`,
`[ocr] use_gpu`); environment variables win.

#### 4-1) Building the GPU image yourself

The image GHCR publishes is **CPU-only**, so build the GPU image locally
(`$C build` above does exactly that). Details:

- Needs x86_64 Linux + Docker — **no GPU required to build**, so you can build
  elsewhere and ship it. ~25GB free disk (6.4GB image + build cache), access to
  PyPI (~2GB of CUDA/cuDNN wheels) and to modelscope.cn (OCR models), ~10 min
  on 8 cores.
- `docker compose -f docker-compose.yml -f docker-compose.gpu.yml build`, or
  `docker build --target gpu -t myphotos:gpu .` (without `--target gpu` you get
  the CPU image). The runtime UID comes from `PUID/PGID`, so no rebuild for that.
- Verify on the GPU host:
  ```bash
  docker run --rm --gpus all --entrypoint python myphotos:gpu \
    -c "import onnxruntime as o; print(o.__version__, o.get_device(), o.get_available_providers())"
  ```
- After `git pull`, rebuild (code is baked in): `$C build && $C --profile ml up -d`,
  then `docker image prune -f && docker builder prune -f` to reclaim space.
- Build elsewhere and ship: `docker save myphotos:gpu | gzip > myphotos-gpu.tar.gz`
  (~2.1GB), copy, `gunzip -c myphotos-gpu.tar.gz | docker load`, then only
  `$C --profile ml up -d` on the GPU host.
- Versions: default onnxruntime-gpu 1.20.2 + CUDA 12.4 + cuDNN 9.1 (driver ≥ 550).
  Change onnxruntime with `--build-arg ORT_GPU_VERSION=…`; to move to another CUDA
  line, bump the `nvidia-*` pins in the Dockerfile together per the onnxruntime
  [compatibility table](https://onnxruntime.ai/docs/execution-providers/CUDA-ExecutionProvider.html).

| Build symptom | Cause / fix |
|---|---|
| `No matching distribution found for onnxruntime-gpu==…` | version not on PyPI — `pip index versions onnxruntime-gpu`, set `ORT_GPU_VERSION` |
| fails at the OCR model step (`_build_v3` / `grep -q korean_PP-OCRv4`) | modelscope.cn blocked — check firewall/proxy (`--build-arg HTTPS_PROXY=…`) |
| `no space left on device` | build cache — `docker builder prune -f`, keep ~25GB free |
| builds fine but logs `CUDA requested but not attached` | host issue, not the image — see 0) |

### 5) Resource usage · speed

Measured on a Turing-class 8GB GPU, 8 vCPU, 8GB RAM VM; 50 public-dataset
photos as 1024px thumbnails (mean ms per photo, sequential, one thread).

| Model | GPU (CUDA) | CPU 1 thread (default) | CPU 8 threads |
|---|---|---|---|
| YOLO objects | **20** | 120 | 79 |
| CLIP embedding (int8) | 41 | 43 | **27** |
| Faces detect+embed | **41** | 73 | 84 |
| OCR (det+rec) | **105** | 353 | 346 |
| Total | **≈ 210** | ≈ 590 | ≈ 535 |

- Full pipeline (ML worker default 2 threads): all four ML stages for 300
  photos in about 50–60 s — **~20k photos/hour**, roughly 3× CPU-only.
- **CLIP gains nothing on the GPU**: the shipped model is int8-quantized, so
  some ops run on CPU with GPU↔CPU copies (the `Memcpy nodes are added` log
  warning). The other three models carry the speed-up.
- OCR (rapidocr) uses all CPU cores on its own, independent of the thread knob.
- GPU utilisation stays around 20%; CPU-side pre/post-processing is the
  bottleneck. `[worker] ml_concurrency = 4` adds ~15–20% throughput at the cost
  of more VRAM/RAM.

| Memory | ML 2 threads (default) | ML 4 threads |
|---|---|---|
| VRAM (all models loaded, busy) | ≈ 2.3GB | ≈ 3.7GB |
| ML worker RAM | ≈ 1.2GB | ≈ 1.5GB |
| API / indexing worker RAM | ≈ 0.2GB / 0.15GB (idle) | |

- One thread with every model loaded: VRAM ≈ 0.9–1.2GB.
- `CUDA_MEM_LIMIT_MB` is **per session** — use **512 or more**; at 256 OCR
  detection fails to allocate on large images. 512 showed no measurable slowdown.
- Disk: image ≈ 6.4GB; `data/` = thumbnails (~0.1–0.3MB per photo, 256px +
  1024px) + `catalog.db` (FTS, embeddings and job history — several GB or more
  on large libraries) + ML models ≈ 200MB. If you have an existing install, size
  from its `du -sh data/*` plus ~50% headroom. Keep the DB on a local SSD.

### 6) Moving an existing install

The only host-specific value in the DB is **`roots.abs_path`** (`photos.rel_path`
is root-relative, thumbnails are addressed by SHA-256).

1. Stop the old services, snapshot the DB — see [porting.md](../operations/porting.md#english).
2. Copy all of `data/` (catalog.db, thumbs, proxies, trash, models,
   session.secret) and `config/local.toml` to the new `DATA_DIR` / `CONFIG_DIR`;
   chown to `PUID:PGID`.
3. Mount the originals (step 1).
4. **Before starting the stack**, rewrite root paths to the in-container paths:
   ```bash
   $C run --rm --no-deps api python -m app.tools.cutover --list
   $C run --rm --no-deps api python -m app.tools.cutover --map "/old/abs/path=/photos" --apply
   ```
   Scanning with wrong paths marks every photo "missing" (recoverable by fixing
   the path and rescanning, but avoid it).
5. `$C --profile ml up -d` — the api runs `alembic upgrade head`.
6. If you moved the trash (step 3), move the old `data/trash/` contents along.

Keep SQLite on **local disk** (not NFS/SMB). Never run the old and new install
against the same originals at the same time. In the GPU image the
`MYPHOTOS_ONNX_PROVIDERS` env overrides `[ml] onnx_providers`.

### Troubleshooting

| Symptom | Check |
|---|---|
| `could not select device driver "nvidia"` | Container Toolkit missing / docker not restarted after `nvidia-ctk runtime configure` |
| `CUDA requested but not attached` in logs | driver ≥ 550? does `docker run --gpus all … nvidia-smi` work? |
| `CUDA_ERROR_OUT_OF_MEMORY` / sharing VRAM | set `CUDA_MEM_LIMIT_MB` (usage ≈ sessions × cap + context) |
| `Permission denied` on some folders | NAS secondary group missing from `PGIDS` / NFS squash |
| OCR `Unsupported rec.lang_type='korean'` | keep `[ocr] ocr_version` set (default `PP-OCRv4`) |
| Deletes are slow / trash full | trash on another mount → step 3 |

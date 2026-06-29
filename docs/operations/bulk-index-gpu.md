# GPU PC에서 초기 백로그 색인 → NAS 적재

초기 색인(특히 ML 자동 분류)은 무겁지만 **일회성**입니다. 처음 한 번을
강력한 GPU PC(예: RTX 4090 Windows)에서 빠르게 끝내고, 완성된 카탈로그를
NAS로 옮겨 운영하면 됩니다. 이후 신규 업로드는 양이 적어 NAS의 CPU로 충분합니다.

> **DB는 SQLite 그대로.** 별도 서버 분리나 DB 전환이 아니라, 같은 체크아웃을
> GPU PC에서 한 번 돌려 `data/`를 만들고 NAS로 복사하는 방식입니다.

## 무엇이 GPU로 빨라지나

| 단계 | 성격 | GPU 효과 |
| --- | --- | --- |
| index (SHA·EXIF·썸네일) | 원본 읽는 **IO 바운드** | 거의 없음 (로컬 디스크가 관건) |
| **classify (YOLO·CLIP·얼굴·OCR)** | 1024px 썸네일만 읽는 **연산 바운드** | **큼** — 백로그의 주범 |
| estimate_location / transcode | 가벼움 / 영상만 | — |

그래서 GPU PC에서는 index까지 포함해 전체를 돌리되, 체감 이득은 classify에서 납니다.

## 1) GPU PC 준비

1. MyPhotos 소스 체크아웃 + 프로젝트 venv 생성 (`docs/install/windows.md` 참고).
2. **GPU용 onnxruntime 설치** (프로젝트 venv에서):
   ```powershell
   pip uninstall -y onnxruntime
   pip install onnxruntime-directml      # Windows·아무 GPU, 설치 간단 (권장)
   # 또는 NVIDIA 최고 성능:  pip install onnxruntime-gpu   (CUDA/cuDNN 필요)
   ```
3. **사진 라이브러리를 PC 로컬 디스크로 복사.** index는 원본을 읽는 IO 바운드라
   네트워크 마운트보다 로컬 NVMe가 훨씬 빠릅니다.
4. `config/local.toml`에 백로그용 설정:
   ```toml
   [ml]
   auto_enqueue = true        # 색인되면 바로 분류까지 자동 진행

   [worker]
   ml_concurrency = 4         # 4090이면 더 올려도 됨
   ```

## 2) 데스크탑 앱으로 색인

1. 데스크탑 앱 → **서버 관리**.
2. **ML 가속 (GPU)** 드롭다운은 기본이 `자동` — 2단계에서 GPU용 onnxruntime를
   깔았으면 그대로 두면 GPU를 자동으로 씁니다. **`GPU 확인`** 으로
   `자동 모드 — GPU 감지됨: … 사용` 메시지를 확인하세요(특정 장치로 고정하고
   싶으면 `DirectML`/`NVIDIA CUDA`를 직접 선택).
   - GPU 설정은 ML 워커에 **환경변수로만** 주입되어 `local.toml`을 건드리지
     않습니다 → NAS로 옮겨도 GPU 설정이 따라가지 않습니다.
3. **전체 시작** → 루트 추가 시 경로는 **슬래시로**(`D:/Photos`) 넣습니다
   (양쪽 OS에서 `join_root`가 안전).
4. **인덱싱 진행 상태** 패널의 대기/실행 큐가 0이 될 때까지 대기.

## 3) NAS로 전송

양쪽 서비스를 정지한 뒤 `data/`를 NAS로 복사합니다. 정합성 있는 스냅샷·전송
명령은 **[porting.md](porting.md)** 1~2절을 그대로 따르세요
(`catalog.db` + `thumbs/` + `proxies/`).

> 전송 시 `config/local.toml`도 가져간다면, GPU 설정을 드롭다운(환경변수)으로
> 했는지 확인하세요. `[ml].onnx_providers`를 toml에 직접 적었다면 그 줄은
> 빼고 옮겨야 NAS가 CPU로 돕니다.

## 4) 경로 재작성 (cutover)

원본 사진을 NAS에 **같은 폴더 구조**로 둔 뒤, 루트 경로만 바꿉니다. `rel_path`는
OS 무관(POSIX+NFC)이라 행별 수정은 필요 없습니다.

```bash
# NAS에서, 서비스 정지 상태로
python -m app.tools.cutover --list                          # 현재 루트 확인
python -m app.tools.cutover --map "D:/Photos=/volume1/photos"   # 미리보기(dry-run)
python -m app.tools.cutover --map "D:/Photos=/volume1/photos" --apply
```

- 기본은 **dry-run** — `--apply` 를 줘야 실제로 기록합니다.
- 새 경로에 원본이 실제로 있는지 **샘플 검증**까지 해줍니다(`--no-verify`로 생략).
- 경로 대신 ID로도 지정 가능: `--id 1=/volume1/photos`.

(서버를 띄운 상태라면 porting.md §4의 admin API PATCH로도 같은 작업을 할 수 있습니다.)

## 5) 시작

NAS 서비스를 시작합니다. 썸네일·임베딩·태그가 모두 이관됐으므로 갤러리·검색이
바로 동작합니다. `auto_enqueue = true`를 유지하면 이후 신규 업로드만 NAS에서
자동 처리됩니다(소량이라 CPU로 충분).

> **얼굴 군집**은 전체 임베딩이 모여야 하는 전역 단계입니다. 보통 색인 중
> 자동으로 처리되지만, 이관 후 얼굴 그룹이 비어 보이면 관리 화면에서 얼굴
> 재군집을 한 번 실행하세요.

---

## English (summary)

Initial indexing — especially ML classification — is heavy but one-time. Do
it once on a strong GPU PC, then ship the finished catalog to the NAS:

1. **GPU PC**: project checkout + venv, `pip install onnxruntime-directml`
   (or `-gpu` for CUDA), copy the photo library to a local disk, set
   `[ml].auto_enqueue=true` and a higher `[worker].ml_concurrency`.
2. **Desktop app** → 서버 관리 → pick the GPU device in the **ML 가속**
   dropdown (injected as an env var, so `local.toml` is untouched and the
   GPU choice never travels to the NAS), **GPU 확인**, then 전체 시작 and let
   the queue drain. Use slash paths for roots (`D:/Photos`).
3. **Transfer** `data/` to the NAS per [porting.md](porting.md) §1–2.
4. **Cutover** — only `roots.abs_path` changes (rel_path is OS-independent):
   ```bash
   python -m app.tools.cutover --map "D:/Photos=/volume1/photos" --apply
   ```
   Dry-run by default; samples the new path to confirm originals are present.
5. **Start** the NAS services. Keep `auto_enqueue` on for the trickle of new
   uploads (CPU is plenty). Re-run face clustering if face groups look empty.

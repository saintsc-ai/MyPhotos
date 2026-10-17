# MyPhotos — Docker image
#
# Single image, runs in three roles via $MYPHOTOS_ROLE:
#   api        → uvicorn app.api.main:app  (default)
#   worker     → python -m app.worker.main
#   ml-worker  → python -m app.worker_ml.main
#
# Build (two variants from this one file):
#   docker build -t myphotos:latest .                       # CPU (default target)
#   docker build --target gpu -t myphotos:gpu .             # NVIDIA GPU (CUDA 12)
# The GPU variant is the CPU image + onnxruntime-gpu, the CUDA 12.4 /
# cuDNN 9 runtime libraries (pip wheels, no CUDA base image needed) and
# RapidOCR. Run it with --gpus / the compose GPU override; see
# docs/install/docker-gpu.md.
#
# python:3.11-slim-bookworm is the safest base for our pinned wheels:
#   - onnxruntime 1.16.x ships manylinux2014 (glibc 2.17+) wheels
#   - tokenizers 0.15–0.19 ships manylinux_2_17 wheels
#   - numpy 1.26.x ships manylinux_2_17 wheels
# Newer Python or distroless bases force source builds we don't want.

FROM python:3.11-slim-bookworm AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUTF8=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    MYPHOTOS_DATA=/app/data \
    MYPHOTOS_CONFIG=/app/config

# System packages:
#   exiftool      — Pentax MakerNote / HEIC / RAW preview extraction
#                   (pulls perl as a hard dep, which the script needs)
#   ffmpeg        — single-frame video thumbnails
#   libheif1      — runtime for pillow-heif wheels (HEIC decode)
#   tini          — proper PID 1 so SIGTERM reaches uvicorn / workers
#   curl          — used by scripts/install-ml-models.sh inside the container
#   ca-certificates — TLS for the above
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
        exiftool \
        ffmpeg \
        libheif1 \
        tini \
        curl \
        ca-certificates \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python dependencies first so the layer is cached across code changes.
# We copy only the metadata, install with `.[heic]` to get pillow-heif, then
# copy the rest of the source. The `-e .` reinstall at the end is cheap (just
# rewires the egg-link) but picks up any new entry points.
COPY pyproject.toml README.md ./
COPY app ./app
RUN pip install -e ".[heic]"

# Remaining tree (alembic migrations, config defaults, scripts).
# Vendor/data are intentionally NOT copied — apt provides exiftool/ffmpeg,
# data/ is a volume mount.
COPY alembic.ini ./
COPY alembic ./alembic
COPY config ./config
COPY scripts ./scripts
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh \
 && chmod +x scripts/*.sh 2>/dev/null || true

# Non-root user. UID/GID overridable at build time so the container's writes
# into /app/data and /photos line up with the host owner (important on NAS
# where photo files are owned by a specific UID).
ARG APP_UID=1000
ARG APP_GID=1000
RUN groupadd --system --gid "${APP_GID}" myphotos \
 && useradd  --system --uid "${APP_UID}" --gid "${APP_GID}" \
        --home-dir /app --shell /usr/sbin/nologin myphotos \
 && mkdir -p /app/data /photos \
 && chown -R myphotos:myphotos /app

USER myphotos

EXPOSE 8888

ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/docker-entrypoint.sh"]
CMD ["api"]


# ---------------------------------------------------------------------------
# GPU variant: docker build --target gpu -t myphotos:gpu .
#
# Version pins must move together:
#   onnxruntime-gpu 1.20.x is built for CUDA 12.x + cuDNN 9.x. The CUDA
#   runtime libs are pinned to 12.4 so the image runs on NVIDIA driver
#   >= 550 (CUDA 12.4); newer drivers run it too. To target a newer
#   CUDA, bump the nvidia-* pins together with onnxruntime-gpu per
#   https://onnxruntime.ai/docs/execution-providers/CUDA-ExecutionProvider.html
FROM base AS gpu

USER root

ARG ORT_GPU_VERSION=1.20.2
# Replace the CPU onnxruntime with the CUDA build (both install the same
# `onnxruntime` module, so the CPU one must go first), then add:
#   - CUDA 12.4 runtime + cuBLAS/cuFFT/cuRAND/NVRTC and cuDNN 9 (pip wheels)
#   - RapidOCR v3 (+ headless OpenCV; numpy stays <2 like the base image)
RUN pip uninstall -y onnxruntime \
 && pip install \
        "onnxruntime-gpu==${ORT_GPU_VERSION}" \
        "nvidia-cuda-runtime-cu12==12.4.127" \
        "nvidia-cuda-nvrtc-cu12==12.4.127" \
        "nvidia-cublas-cu12==12.4.5.8" \
        "nvidia-cufft-cu12==11.2.1.3" \
        "nvidia-curand-cu12==10.3.5.147" \
        "nvidia-cudnn-cu12==9.1.0.70" \
        "rapidocr>=3.0" \
        "opencv-python-headless<4.12" \
        "numpy>=1.24,<2"

# onnxruntime 1.20 dlopens libcudart/libcublas/libcudnn by soname, so the
# pip-installed library dirs must be on the loader path.
ENV LD_LIBRARY_PATH=/usr/local/lib/python3.11/site-packages/nvidia/cuda_runtime/lib:/usr/local/lib/python3.11/site-packages/nvidia/cuda_nvrtc/lib:/usr/local/lib/python3.11/site-packages/nvidia/cublas/lib:/usr/local/lib/python3.11/site-packages/nvidia/cufft/lib:/usr/local/lib/python3.11/site-packages/nvidia/curand/lib:/usr/local/lib/python3.11/site-packages/nvidia/cudnn/lib

# RapidOCR fetches its det/cls/rec models into its package dir on first
# use, which a non-root user can't write. Fetch the set the default [ocr]
# config asks for (Korean, PP-OCRv4) through the app's own builder — det /
# cls / rec each load lazily on first use (rec only once text is found), so
# poke rapidocr's loaders — then open the dir to any UID (PUID may differ
# from the image user) so other lang/version values can still be fetched
# at runtime. The grep fails the build if the Korean rec model is missing.
RUN MYPHOTOS_ONNX_PROVIDERS=CPUExecutionProvider \
    python -c "from app.worker_ml.ocr import _build_v3; e = _build_v3(); [getattr(e, f)() for f in ('_load_det_model', '_load_cls_model', '_load_rec_model') if hasattr(e, f)]" \
 && ls /usr/local/lib/python3.11/site-packages/rapidocr/models | grep -q korean_PP-OCRv4 \
 && chmod -R a+rwX /usr/local/lib/python3.11/site-packages/rapidocr/models

# GPU first, CPU fallback. Overrides [ml].onnx_providers — set this env to
# "CPUExecutionProvider" (compose) to force CPU without rebuilding. If the
# GPU isn't usable at runtime the worker logs it and runs on CPU.
ENV MYPHOTOS_ONNX_PROVIDERS=CUDAExecutionProvider,CPUExecutionProvider \
    NVIDIA_VISIBLE_DEVICES=all \
    NVIDIA_DRIVER_CAPABILITIES=compute,utility

USER myphotos


# ---------------------------------------------------------------------------
# Default target (plain `docker build .`): the CPU image, unchanged.
FROM base AS cpu

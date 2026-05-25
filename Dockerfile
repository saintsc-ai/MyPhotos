# MyPhotos — Docker image
#
# Single image, runs in three roles via $MYPHOTOS_ROLE:
#   api        → uvicorn app.api.main:app  (default)
#   worker     → python -m app.worker.main
#   ml-worker  → python -m app.worker_ml.main
#
# Build:
#   docker build -t myphotos:latest .
#
# python:3.11-slim-bookworm is the safest base for our pinned wheels:
#   - onnxruntime 1.16.x ships manylinux2014 (glibc 2.17+) wheels
#   - tokenizers 0.15–0.19 ships manylinux_2_17 wheels
#   - numpy 1.26.x ships manylinux_2_17 wheels
# Newer Python or distroless bases force source builds we don't want.

FROM python:3.11-slim-bookworm AS runtime

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

#!/usr/bin/env bash
# MyPhotos container entrypoint.
#
# Dispatches on the first argument (or $MYPHOTOS_ROLE):
#   api        → uvicorn (default)
#   worker     → indexing worker
#   ml-worker  → ML worker (YOLO/CLIP/face)
#   shell      → drop into bash for debugging
#   <anything else> → exec verbatim
#
# Runs `alembic upgrade head` exactly once per container start, only in the
# api role — the SQLite DB is shared across all three roles via the data
# volume, so serializing migrations through one process is enough.

set -euo pipefail

ROLE="${1:-${MYPHOTOS_ROLE:-api}}"
shift || true

# Ensure runtime dirs exist on the data volume. The Python code also does this
# on import, but doing it here gives a clear early failure if the volume is
# read-only or owned by the wrong UID.
mkdir -p \
    "${MYPHOTOS_DATA:-/app/data}" \
    "${MYPHOTOS_DATA:-/app/data}/thumbs" \
    "${MYPHOTOS_DATA:-/app/data}/proxies" \
    "${MYPHOTOS_DATA:-/app/data}/logs" \
    "${MYPHOTOS_DATA:-/app/data}/state" \
    "${MYPHOTOS_DATA:-/app/data}/trash" \
    "${MYPHOTOS_DATA:-/app/data}/tmp"

case "$ROLE" in
  api)
    # API is the migration owner. Worker / ml-worker wait on the API service
    # (via compose `depends_on`) so by the time they start the schema is ready.
    echo "==> alembic upgrade head"
    python -m alembic upgrade head

    # Honor server.port from config if set, otherwise the documented default.
    PORT="${MYPHOTOS_PORT:-8888}"
    HOST="${MYPHOTOS_HOST:-0.0.0.0}"
    WORKERS="${MYPHOTOS_API_WORKERS:-2}"

    echo "==> starting API on ${HOST}:${PORT} (workers=${WORKERS})"
    exec uvicorn app.api.main:app \
        --host "${HOST}" \
        --port "${PORT}" \
        --workers "${WORKERS}" \
        "$@"
    ;;

  worker)
    echo "==> starting indexing worker"
    exec python -m app.worker.main "$@"
    ;;

  ml-worker)
    echo "==> starting ML worker"
    exec python -m app.worker_ml.main "$@"
    ;;

  shell|bash|sh)
    exec bash
    ;;

  *)
    # Allow ad-hoc commands: `docker run myphotos alembic current` etc.
    exec "$ROLE" "$@"
    ;;
esac

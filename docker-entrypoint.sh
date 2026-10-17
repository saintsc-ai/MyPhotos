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

# Optional runtime identity. When the container starts as root (compose
# `user: "0:0"`), drop to PUID:PGID plus the supplementary groups in PGIDS
# (comma-separated GIDs) before doing anything else — for NAS shares whose
# folders are only reachable through a secondary group. UMASK (e.g. 0002)
# keeps new files group-writable. Without root this block is skipped and the
# image's built-in user (APP_UID/APP_GID) is used, as before.
if [ "$(id -u)" = "0" ] && [ -z "${MYPHOTOS_PRIVS_DROPPED:-}" ]; then
    PUID="${PUID:-1000}"
    PGID="${PGID:-${PUID}}"
    PGIDS="$(printf '%s' "${PGIDS:-}" | tr -d ' ')"
    for v in "$PUID" "$PGID"; do
        case "$v" in ''|*[!0-9]*) echo "PUID/PGID must be numeric (got '$v')" >&2; exit 64 ;; esac
    done
    case "$PGIDS" in *[!0-9,]*) echo "PGIDS must be comma-separated numeric GIDs (got '$PGIDS')" >&2; exit 64 ;; esac
    GROUPS_CSV="$PGID${PGIDS:+,$PGIDS}"
    # Create the runtime dirs and hand just those directories (not their
    # contents, not the photos) to the runtime user — a fresh volume is
    # root-owned and the image's /app/data belongs to APP_UID. Existing
    # files (catalog.db, thumbnails) keep their owner: chown those yourself
    # when changing PUID on an existing install.
    DATA="${MYPHOTOS_DATA:-/app/data}"
    for d in "$DATA" "$DATA/thumbs" "$DATA/proxies" "$DATA/logs" "$DATA/state" \
             "$DATA/tmp" "${MYPHOTOS_TRASH_DIR:-$DATA/trash}"; do
        mkdir -p "$d"
        if [ "$(stat -c %u "$d")" != "$PUID" ]; then
            chown "$PUID:$PGID" "$d" || echo "warning: could not chown $d" >&2
        fi
    done
    echo "==> running as uid=$PUID gid=$PGID groups=$GROUPS_CSV"
    export MYPHOTOS_PRIVS_DROPPED=1 HOME=/app
    exec setpriv --reuid="$PUID" --regid="$PGID" --groups="$GROUPS_CSV" \
        --inh-caps=-all -- "$0" "$ROLE" "$@"
fi
if [ -n "${UMASK:-}" ]; then
    umask "$UMASK"
fi

# Ensure runtime dirs exist on the data volume. The Python code also does this
# on import, but doing it here gives a clear early failure if the volume is
# read-only or owned by the wrong UID.
mkdir -p \
    "${MYPHOTOS_DATA:-/app/data}" \
    "${MYPHOTOS_DATA:-/app/data}/thumbs" \
    "${MYPHOTOS_DATA:-/app/data}/proxies" \
    "${MYPHOTOS_DATA:-/app/data}/logs" \
    "${MYPHOTOS_DATA:-/app/data}/state" \
    "${MYPHOTOS_TRASH_DIR:-${MYPHOTOS_DATA:-/app/data}/trash}" \
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

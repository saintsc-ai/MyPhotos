#!/usr/bin/env bash
# Bootstrap a MyPhotos checkout on Linux (Synology DSM, etc.)
#
# Prefers uv if available; falls back to plain python -m venv + pip.
#
# Re-runnable. Does not touch data/ if catalog.db already exists.
#
# Environment overrides:
#   PYTHON_VERSION  python version uv should use (default: from .python-version, then 3.11.9)
#   PYTHON_BIN      explicit interpreter path (used in fallback path)

set -euo pipefail

cd "$(dirname "$0")/.."
APP_DIR="$(pwd)"
echo "==> bootstrapping in $APP_DIR"

PY_VERSION="${PYTHON_VERSION:-}"
if [ -z "$PY_VERSION" ] && [ -f .python-version ]; then
  PY_VERSION="$(tr -d '[:space:]' < .python-version)"
fi
PY_VERSION="${PY_VERSION:-3.11.9}"

if command -v uv >/dev/null 2>&1; then
  echo "==> uv detected ($(uv --version)); using uv path"

  # Ensure the requested Python is installed (no-op if already present)
  uv python install "$PY_VERSION"

  # (Re)create venv if missing
  if [ ! -d .venv ]; then
    uv venv --python "$PY_VERSION" .venv
  fi

  # Install project (editable) into .venv
  uv pip install --python .venv/bin/python -e .

  # Run migrations using the venv directly (no activation needed)
  .venv/bin/python -m alembic upgrade head

else
  echo "==> uv not found; falling back to python -m venv"

  PYTHON_BIN="${PYTHON_BIN:-}"
  if [ -z "$PYTHON_BIN" ]; then
    for candidate in python3.13 python3.12 python3.11 python3; do
      if command -v "$candidate" >/dev/null 2>&1; then
        ver=$("$candidate" -c "import sys; print('%d.%d' % sys.version_info[:2])")
        case "$ver" in
          3.11|3.12|3.13|3.14|3.15)
            PYTHON_BIN="$(command -v "$candidate")"
            break
            ;;
        esac
      fi
    done
  fi
  if [ -z "$PYTHON_BIN" ]; then
    echo "ERROR: no Python 3.11+ found." >&2
    echo "  Install uv (https://docs.astral.sh/uv/) or set PYTHON_BIN=/path/to/python3.11+" >&2
    exit 1
  fi
  echo "==> using $PYTHON_BIN ($($PYTHON_BIN --version))"

  if [ ! -d .venv ]; then
    "$PYTHON_BIN" -m venv .venv
  fi
  # shellcheck disable=SC1091
  . .venv/bin/activate
  python -m pip install -U pip wheel
  pip install -e .
  alembic upgrade head
fi

# Local config
if [ ! -f config/local.toml ]; then
  cp config/local.example.toml config/local.toml
  echo "==> created config/local.toml — edit it before starting the API"
fi

# Mark vendor binaries executable (if present)
for d in vendor/linux-x64 vendor/linux-arm64; do
  if [ -d "$d" ]; then
    find "$d" -maxdepth 1 -type f ! -name '.*' -exec chmod +x {} \; 2>/dev/null || true
  fi
done

mkdir -p data/thumbs data/logs data/state data/trash

echo "==> bootstrap complete"
echo "    Next: edit config/local.toml, then run scripts/install-systemd.sh"

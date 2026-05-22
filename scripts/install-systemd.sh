#!/usr/bin/env bash
# Install MyPhotos systemd units into /etc/systemd/system/.
#
# Environment overrides:
#   APP_DIR    target app directory   (default: parent of this script)
#   APP_USER   service user           (default: current user, or 'root')
#   APP_GROUP  service group          (default: 'users')
#
# Examples:
#   ./scripts/install-systemd.sh                          # current user
#   sudo APP_USER=root ./scripts/install-systemd.sh       # run as root
#   sudo APP_USER=scsung ./scripts/install-systemd.sh
#
# After install:
#   sudo systemctl enable --now myphotos-api myphotos-worker
#   sudo systemctl status myphotos-api myphotos-worker
#   journalctl -u myphotos-worker -f

set -euo pipefail

APP_DIR="${APP_DIR:-$(cd "$(dirname "$0")/.." && pwd)}"
APP_USER="${APP_USER:-$(id -un)}"
APP_GROUP="${APP_GROUP:-users}"

TEMPLATE_DIR="$APP_DIR/systemd"
TARGET_DIR="/etc/systemd/system"

if [ ! -d "$TEMPLATE_DIR" ]; then
  echo "ERROR: $TEMPLATE_DIR not found" >&2
  exit 1
fi

if [ ! -x "$APP_DIR/.venv/bin/uvicorn" ]; then
  echo "ERROR: $APP_DIR/.venv/bin/uvicorn not found." >&2
  echo "  Run scripts/bootstrap.sh first." >&2
  exit 1
fi

echo "==> installing systemd units"
echo "    APP_DIR  = $APP_DIR"
echo "    APP_USER = $APP_USER"
echo "    APP_GROUP= $APP_GROUP"

needs_sudo=""
if [ ! -w "$TARGET_DIR" ]; then
  needs_sudo="sudo"
fi

for tmpl in "$TEMPLATE_DIR"/*.service.in; do
  unit=$(basename "$tmpl" .in)
  rendered=$(mktemp)
  sed \
    -e "s|@APP_DIR@|$APP_DIR|g" \
    -e "s|@APP_USER@|$APP_USER|g" \
    -e "s|@APP_GROUP@|$APP_GROUP|g" \
    "$tmpl" > "$rendered"
  $needs_sudo install -m 0644 "$rendered" "$TARGET_DIR/$unit"
  rm -f "$rendered"
  echo "    installed $TARGET_DIR/$unit"
done

$needs_sudo systemctl daemon-reload
echo "==> done. Enable with:"
echo "    sudo systemctl enable --now myphotos-api myphotos-worker"

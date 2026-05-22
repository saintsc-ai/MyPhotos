#!/usr/bin/env bash
# Stop, disable, and remove MyPhotos systemd units.

set -euo pipefail

TARGET_DIR="/etc/systemd/system"
UNITS=(myphotos-api.service myphotos-worker.service)

needs_sudo=""
if [ ! -w "$TARGET_DIR" ]; then
  needs_sudo="sudo"
fi

for unit in "${UNITS[@]}"; do
  if [ -f "$TARGET_DIR/$unit" ]; then
    $needs_sudo systemctl stop "$unit" 2>/dev/null || true
    $needs_sudo systemctl disable "$unit" 2>/dev/null || true
    $needs_sudo rm -f "$TARGET_DIR/$unit"
    echo "    removed $TARGET_DIR/$unit"
  fi
done

$needs_sudo systemctl daemon-reload
echo "==> done"

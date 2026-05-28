#!/bin/sh
# Set Linux POSIX permissions on a photo root so MyPhotos can read every
# file AND write per-folder (needed for rotation + trash). Use this on a
# Synology share whose top folder was created by Synology Photos and
# defaults to "ACL-only" (`d---------+`), which blocks our service user
# from doing anything.
#
# Usage:
#   sudo ./scripts/fix-photo-perms.sh                       # /volume1/photo, $SUDO_USER
#   sudo ./scripts/fix-photo-perms.sh /volume1/photo
#   sudo ./scripts/fix-photo-perms.sh /volume1/photo scsung
#
# What it does:
#   chown -R <user>:users <root>          # makes <user> the owner
#   chmod -R u+rwX,g+rX,o+rX <root>       # owner can rw, all can read
#                                           (capital X = exec/enter on
#                                           DIRECTORIES only, not files)
#
# Idempotent. Re-running is safe.
#
# After:
#   - color photos browse fine (read)
#   - Synology Photos still works (it owns the indices independently)
#   - MyPhotos rotation succeeds (exiftool can create
#     <file>_exiftool_tmp in the same dir)
#   - MyPhotos delete succeeds (shutil.move can drop the file entry
#     from the source dir)

set -e

ROOT="${1:-/volume1/photo}"
TARGET_USER="${2:-${SUDO_USER:-$USER}}"

if [ "$(id -u)" -ne 0 ]; then
  echo "Run with sudo — this needs root to chown/chmod /volume1/*." >&2
  exit 1
fi

if [ ! -d "$ROOT" ]; then
  echo "Directory not found: $ROOT" >&2
  exit 1
fi

if ! id -u "$TARGET_USER" >/dev/null 2>&1; then
  echo "User does not exist: $TARGET_USER" >&2
  exit 1
fi

echo "Photo root : $ROOT"
echo "Owner user : $TARGET_USER (group: users)"
echo

# Synology shares may have native ACL bits; chmod doesn't always wipe
# them. Quick probe — if we see synoacltool installed and the root has
# ACL entries, warn the user.
if command -v synoacltool >/dev/null 2>&1; then
  acl_status="$(synoacltool -get "$ROOT" 2>&1 | head -1 || true)"
  case "$acl_status" in
    *"Linux mode"*) ;;
    "")             ;;
    *)
      echo "⚠ Synology ACL detected on $ROOT:"
      echo "    $acl_status"
      echo "  POSIX chmod may not fully apply. If permissions still fail"
      echo "  after this script, fix via DSM Control Panel → 공유 폴더 →"
      echo "  edit $ROOT → 권한 탭 → grant '$TARGET_USER' read/write +"
      echo "  '이 폴더, 하위 폴더 및 파일에 적용'."
      echo
      ;;
  esac
fi

echo "Counting entries (may take a few seconds on big libraries)..."
n="$(find "$ROOT" 2>/dev/null | wc -l)"
echo "  $n entries under $ROOT"
echo

echo "→ chown -R $TARGET_USER:users $ROOT"
chown -R "$TARGET_USER":users "$ROOT"
echo "→ chmod -R u+rwX,g+rX,o+rX $ROOT"
chmod -R u+rwX,g+rX,o+rX "$ROOT"

echo
echo "Done. Sanity check:"
ls -ld "$ROOT" | sed 's/^/  /'
ls -ld "$ROOT"/*/ 2>/dev/null | head -5 | sed 's/^/  /'
echo
echo "Now restart the API so it picks up the new permissions:"
echo "  sudo systemctl restart myphotos-api"

#!/usr/bin/env bash
# Download exiftool + ffmpeg into vendor/linux-x64/ so the worker can find them
# without touching the host. Re-runnable.
#
# Run on the NAS:
#   cd ~/myphotos
#   ./scripts/install-vendor-linux-x64.sh

set -euo pipefail

cd "$(dirname "$0")/.."
VENDOR="vendor/linux-x64"
mkdir -p "$VENDOR"
cd "$VENDOR"

# ---- ExifTool ----------------------------------------------------------------
# Single-binary distribution (still needs system perl, which DSM provides).
# Version can be overridden:  EXIFTOOL_VER=13.10 ./scripts/install-vendor-linux-x64.sh
# Otherwise we ask exiftool.org for the latest published version.
if [ -z "${EXIFTOOL_VER:-}" ]; then
  EXIFTOOL_VER="$(curl -fsS https://exiftool.org/ver.txt 2>/dev/null | tr -d '[:space:]' || true)"
fi
EXIFTOOL_VER="${EXIFTOOL_VER:-13.00}"
EXIFTOOL_URL="https://exiftool.org/Image-ExifTool-${EXIFTOOL_VER}.tar.gz"
echo "==> exiftool target version: ${EXIFTOOL_VER}"

if [ ! -x exiftool ]; then
  echo "==> downloading exiftool ${EXIFTOOL_VER}"
  curl -fLO "$EXIFTOOL_URL"
  tar xzf "Image-ExifTool-${EXIFTOOL_VER}.tar.gz"
  # The standalone script looks for its modules in a sibling 'lib/'
  # directory — do NOT rename or it will die with 'Can't locate Image/...'.
  rm -rf lib
  mv "Image-ExifTool-${EXIFTOOL_VER}/lib" lib
  mv "Image-ExifTool-${EXIFTOOL_VER}/exiftool" exiftool
  rm -rf "Image-ExifTool-${EXIFTOOL_VER}" "Image-ExifTool-${EXIFTOOL_VER}.tar.gz"
  chmod +x exiftool
fi
./exiftool -ver > /dev/null && echo "    exiftool OK ($(./exiftool -ver))"

# ---- ffmpeg ------------------------------------------------------------------
# Static build from johnvansickle (x86_64 glibc).
FFMPEG_URL="https://johnvansickle.com/ffmpeg/releases/ffmpeg-release-amd64-static.tar.xz"

if [ ! -x ffmpeg ]; then
  echo "==> downloading ffmpeg static build"
  curl -fLO "$FFMPEG_URL"
  tar xJf ffmpeg-release-amd64-static.tar.xz
  mv ffmpeg-*-amd64-static/ffmpeg .
  mv ffmpeg-*-amd64-static/ffprobe . 2>/dev/null || true
  rm -rf ffmpeg-*-amd64-static ffmpeg-release-amd64-static.tar.xz
  chmod +x ffmpeg ffprobe 2>/dev/null || true
fi
./ffmpeg -version | head -1 && echo "    ffmpeg OK"

echo "==> vendor/linux-x64 ready"
ls -la

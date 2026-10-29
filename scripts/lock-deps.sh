#!/usr/bin/env bash
# Regenerate the hash-pinned dependency lock the Docker image installs from.
#
#   scripts/lock-deps.sh            # re-resolve within pyproject.toml bounds
#   scripts/lock-deps.sh --upgrade  # take the newest allowed versions
#
# Universal lock for Python 3.11: hashes cover every platform's wheel, since
# CI builds the image for linux/amd64 and linux/arm64 from the same file.
# Review the diff, rebuild, and commit the lock together with any
# pyproject.toml change. Run monthly (or on a security
# advisory) so the image doesn't sit on stale, vulnerable wheels.

set -euo pipefail
cd "$(dirname "$0")/.."

extra=()
[ "${1:-}" = "--upgrade" ] && extra+=(--upgrade)

uv pip compile pyproject.toml requirements/image.in \
  --extra heic \
  --python-version 3.11 \
  --universal \
  --generate-hashes \
  --no-header \
  "${extra[@]}" \
  -o requirements/docker.lock.txt

echo "==> requirements/docker.lock.txt updated"

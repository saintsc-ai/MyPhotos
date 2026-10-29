#!/usr/bin/env bash
# Pre-deploy vulnerability gate. Exits non-zero when the image must not ship.
#
#   scripts/predeploy-scan.sh <image>          e.g. myphotos:gpu-3a98faf
#
# Checks (all run in throwaway containers — the host needs only docker):
#   1. pip-audit over requirements/docker.lock.txt — any known-vulnerable
#      locked Python package fails. Fast, runs before the image scan.
#   2. Trivy over the built image — OS packages (ffmpeg / exiftool / libheif,
#      the parsers that see untrusted files) AND every Python package actually
#      in the image, including the GPU stage's onnxruntime-gpu / CUDA wheels /
#      rapidocr that the lock doesn't cover. Fails on HIGH or CRITICAL that
#      have a fix available (--ignore-unfixed: an unfixable Debian CVE can't
#      be acted on, so it would only train people to bypass the gate).
#      The same pass runs the secret scanner — a key or token baked into a
#      layer fails too.
#
# Accepting a finding: add its ID to .trivyignore with a comment saying why
# and until when. Emergency override (logged, not silent):
#   ALLOW_VULNS="reason" scripts/predeploy-scan.sh <image>

set -euo pipefail
cd "$(dirname "$0")/.."

IMAGE="${1:?usage: $0 <image>}"
TRIVY_IMAGE="${TRIVY_IMAGE:-aquasec/trivy:0.58.1}"
PY_IMAGE="${PY_IMAGE:-python:3.11-slim-bookworm}"
REPO="$(pwd)"
fail=0

echo "==> [1/2] pip-audit: requirements/docker.lock.txt"
if ! docker run --rm -v "$REPO/requirements:/req:ro" "$PY_IMAGE" sh -c \
    'pip install --quiet --disable-pip-version-check "pip-audit>=2.7" \
     && pip-audit --require-hashes --disable-pip -r /req/docker.lock.txt'; then
  echo "!! pip-audit found vulnerable locked packages (fix: scripts/lock-deps.sh --upgrade)"
  fail=1
fi

trivy() {
  # Cache the vulnerability DB in a named volume so repeat scans don't
  # re-download it; the docker socket lets Trivy read the local image.
  docker run --rm \
    -v /var/run/docker.sock:/var/run/docker.sock \
    -v myphotos-trivy-cache:/root/.cache \
    -v "$REPO/.trivyignore:/work/.trivyignore:ro" \
    "$TRIVY_IMAGE" "$@"
}

# One Trivy pass for vulnerabilities AND secrets: the image is several GB
# and unpacking its layers is most of the scan time, so don't do it twice.
# The severity filter covers both (Trivy's built-in secret rules rate keys
# and tokens HIGH/CRITICAL); the findings table says which kind tripped.
echo "==> [2/2] Trivy vulnerabilities (HIGH/CRITICAL with a fix) + secrets: $IMAGE"
if ! trivy image --quiet --scanners vuln,secret \
    --severity HIGH,CRITICAL --ignore-unfixed \
    --ignorefile /work/.trivyignore \
    --exit-code 1 "$IMAGE"; then
  echo "!! fixable HIGH/CRITICAL vulnerabilities or secret material in the image"
  echo "   OS packages: rebuild with --pull --no-cache to pick up Debian fixes"
  echo "   Python:      scripts/lock-deps.sh --upgrade (or bump the GPU-stage pins)"
  echo "   Secret:      remove it from the build context / layer, then rotate it"
  fail=1
fi

if [ "$fail" -ne 0 ]; then
  if [ -n "${ALLOW_VULNS:-}" ]; then
    echo "==> GATE OVERRIDDEN by ALLOW_VULNS: ${ALLOW_VULNS}"
    exit 0
  fi
  echo "==> BLOCKED: do not deploy $IMAGE"
  exit 1
fi
echo "==> PASSED: $IMAGE"

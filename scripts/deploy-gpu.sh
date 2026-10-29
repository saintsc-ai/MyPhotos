#!/usr/bin/env bash
# Deploy a commit to the GPU Docker host — build, vulnerability gate, swap.
#
#   scripts/deploy-gpu.sh <deploy-dir> <commit> [--migrate]
#
# Run from anywhere on the GPU host. <deploy-dir> is the compose directory
# (docker-compose.yml + .env + config/ + data/ + src/ — see CLAUDE.md).
#
# Order matters: the running containers are only touched AFTER the new image
# has passed scripts/predeploy-scan.sh. A blocked scan leaves production on
# the old image, untouched.
#
#   1. checkout <commit> in <deploy-dir>/src
#   2. docker build --pull --target gpu  → myphotos:gpu-<short>
#   3. scripts/predeploy-scan.sh         → abort on fixable HIGH/CRITICAL,
#                                          vulnerable locked deps, or secrets
#   4. migrations between the running revision and <commit>? abort unless
#      --migrate, which stops workers, backs up catalog.db, starts api alone
#      (it runs `alembic upgrade head`), then the rest
#   5. back up .env, point IMAGE at the new tag, docker compose up -d
#   6. ./status.sh if present
#
# Rollback: restore .env.bak-<old> and `docker compose up -d`.

set -euo pipefail

# The whole body is one { …; exit; } block: this script usually lives in
# <deploy-dir>/src and step 1 checks out another commit over it. Bash reads
# a compound command completely before running it, so the checkout can't
# change the code mid-run.
{
DEPLOY_DIR="${1:?usage: $0 <deploy-dir> <commit> [--migrate]}"
COMMIT="${2:?usage: $0 <deploy-dir> <commit> [--migrate]}"
MIGRATE="${3:-}"
cd "$DEPLOY_DIR"
DEPLOY_DIR="$(pwd)"
[ -f .env ] && [ -d src/.git ] || { echo "not a deploy dir: $DEPLOY_DIR"; exit 2; }

echo "==> [1/6] checkout $COMMIT"
git -C src fetch -q
git -C src checkout -q "$COMMIT"
SHORT="$(git -C src rev-parse --short=7 HEAD)"
FULL="$(git -C src rev-parse HEAD)"
TAG="myphotos:gpu-$SHORT"

OLD_IMAGE="$(sed -n 's/^IMAGE=//p' .env | tail -1)"
OLD_REV=""
if [ -n "$OLD_IMAGE" ]; then
  OLD_REV="$(docker image inspect -f '{{ index .Config.Labels "org.opencontainers.image.revision" }}' \
             "$OLD_IMAGE" 2>/dev/null || true)"
fi
echo "    running: ${OLD_IMAGE:-?} (${OLD_REV:-unknown rev})  →  $TAG"

echo "==> [2/6] build $TAG"
docker build --pull --target gpu \
  --label "org.opencontainers.image.revision=$FULL" \
  -t "$TAG" src

echo "==> [3/6] pre-deploy vulnerability gate"
if ! src/scripts/predeploy-scan.sh "$TAG"; then
  echo "==> ABORTED before touching production. Still running: ${OLD_IMAGE:-?}"
  exit 1
fi

echo "==> [4/6] migration check"
NEEDS_MIGRATION=0
if [ -n "$OLD_REV" ] && git -C src cat-file -e "$OLD_REV^{commit}" 2>/dev/null; then
  if [ -n "$(git -C src diff --name-only "$OLD_REV" "$FULL" -- alembic/versions)" ]; then
    NEEDS_MIGRATION=1
  fi
else
  echo "    running revision unknown — assuming a migration may be needed"
  NEEDS_MIGRATION=1
fi
if [ "$NEEDS_MIGRATION" -eq 1 ] && [ "$MIGRATE" != "--migrate" ]; then
  echo "==> STOP: schema migrations between ${OLD_REV:-?} and $SHORT."
  echo "    Re-run with --migrate (stops workers, backs up data/catalog.db first)."
  echo "    The scanned image $TAG is built and kept; nothing was changed."
  exit 3
fi

echo "==> [5/6] switch IMAGE → $TAG"
cp .env ".env.bak-${OLD_IMAGE##*:}"
sed -i "s|^IMAGE=.*|IMAGE=$TAG|" .env

if [ "$NEEDS_MIGRATION" -eq 1 ]; then
  echo "    migration path: stop → backup → api (alembic) → rest"
  docker compose stop worker ml-worker api
  BK="data/catalog.db.bak-$(date +%Y%m%d-%H%M%S)-${OLD_IMAGE##*:}"
  # Online-safe copy via SQLite's backup API (handles the WAL), using the
  # new image's Python so the host needs nothing installed.
  # Run as the DB file's owner (PUID may differ from the image user).
  docker run --rm --user "$(stat -c %u:%g data/catalog.db)" \
    -v "$DEPLOY_DIR/data:/d" --entrypoint python "$TAG" -c \
    "import sqlite3,sys; s=sqlite3.connect('/d/catalog.db'); d=sqlite3.connect(sys.argv[1]); s.backup(d); d.close(); s.close()" \
    "/d/${BK#data/}"
  echo "    backup: $BK"
  docker compose up -d api
  api_id="$(docker compose ps -q api)"
  st=starting
  for _ in $(seq 1 60); do
    st="$(docker inspect -f '{{.State.Health.Status}}' "$api_id" 2>/dev/null || echo starting)"
    [ "$st" = "healthy" ] && break
    sleep 5
  done
  [ "$st" = "healthy" ] || { echo "!! api not healthy after migration — check: docker compose logs api"; exit 4; }
fi
docker compose up -d

echo "==> [6/6] status"
if [ -x ./status.sh ]; then ./status.sh; else docker compose ps; fi
echo "==> deployed $TAG  (rollback: cp .env.bak-${OLD_IMAGE##*:} .env && docker compose up -d)"
exit 0
}

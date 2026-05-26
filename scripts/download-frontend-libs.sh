#!/usr/bin/env bash
# Pull the JS libraries used by the admin/gallery pages into
# app/web/static/js. Designed for restricted networks: once
# downloaded, the app never talks to a CDN again.
#
# Re-runnable; skips files that already exist (use --force to refresh).
#
# Versions are pinned so a fresh checkout doesn't end up on a different
# Chart.js with subtly different API. Bump deliberately, verify the
# admin page donuts still render before pushing.

set -euo pipefail
cd "$(dirname "$0")/.."

FORCE=0
for a in "$@"; do
  [ "$a" = "--force" ] && FORCE=1
done

JS_DIR="app/web/static/js"
mkdir -p "$JS_DIR"

# Pinned versions — bump together and test.
CHARTJS_VER="4.4.7"
DATALABELS_VER="2.2.0"

fetch() {
  local url="$1" out="$2"
  if [ -f "$out" ] && [ "$FORCE" -ne 1 ]; then
    echo "  exists: $out (skip; --force to refresh)"
    return 0
  fi
  echo "  downloading: $out"
  curl -fLo "$out.part" "$url"
  mv "$out.part" "$out"
}

echo "==> Chart.js ${CHARTJS_VER}"
fetch "https://cdn.jsdelivr.net/npm/chart.js@${CHARTJS_VER}/dist/chart.umd.min.js" \
      "$JS_DIR/chart.umd.min.js"

echo "==> chartjs-plugin-datalabels ${DATALABELS_VER}"
fetch "https://cdn.jsdelivr.net/npm/chartjs-plugin-datalabels@${DATALABELS_VER}/dist/chartjs-plugin-datalabels.min.js" \
      "$JS_DIR/chartjs-plugin-datalabels.min.js"

echo "==> done. Files installed under $JS_DIR"
ls -la "$JS_DIR" | grep -E '\.js$' || true

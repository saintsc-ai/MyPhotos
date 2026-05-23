#!/usr/bin/env bash
# Maintainer-only: upload local data/models/* to the project's GitHub
# Release so public users can fetch them without HuggingFace auth.
#
# Prereqs (one-time):
#   1. Install GitHub CLI:  https://cli.github.com/
#   2. `gh auth login`      (any account with push to the repo)
#   3. Run install-ml-models.sh once locally (with HF_TOKEN if needed)
#      to populate data/models/.
#
# Then:
#   ./scripts/upload-ml-models.sh
#
# Re-running is safe — `gh release upload --clobber` overwrites existing
# assets. Use this to roll out a model upgrade.
#
# Env overrides:
#   MYPHOTOS_RELEASE_TAG   — release tag (default: models-v1)
#   MYPHOTOS_RELEASE_REPO  — owner/name  (default: saintsc-ai/MyPhotos)
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MODELS_DIR="$ROOT/data/models"
TAG="${MYPHOTOS_RELEASE_TAG:-models-v1}"
REPO="${MYPHOTOS_RELEASE_REPO:-saintsc-ai/MyPhotos}"

if ! command -v gh >/dev/null 2>&1; then
  echo "!! gh CLI not found. Install from https://cli.github.com/" >&2
  exit 1
fi

# Map: local relative path  ->  release asset name (flat).
# Keep in sync with install-ml-models.sh.
ASSETS=(
  "yolo/yolov8n.onnx           yolov8n.onnx"
  "clip/vision_quantized.onnx  clip_vision_quantized.onnx"
  "clip/text_quantized.onnx    clip_text_quantized.onnx"
  "clip/tokenizer.json         clip_tokenizer.json"
  "face/yunet.onnx             yunet.onnx"
  "face/sface.onnx             sface.onnx"
)

# Verify all files exist before doing anything network-y.
missing=0
for entry in "${ASSETS[@]}"; do
  rel="$(echo "$entry" | awk '{print $1}')"
  if [ ! -f "$MODELS_DIR/$rel" ]; then
    echo "!! missing: $MODELS_DIR/$rel" >&2
    missing=1
  fi
done
if [ "$missing" -ne 0 ]; then
  echo
  echo "Run scripts/install-ml-models.sh first (HF_TOKEN=... may be needed)." >&2
  exit 1
fi

# Create the release on demand (idempotent).
if ! gh release view "$TAG" --repo "$REPO" >/dev/null 2>&1; then
  echo "==> Creating release $TAG in $REPO"
  gh release create "$TAG" \
    --repo "$REPO" \
    --title "ML model weights ($TAG)" \
    --notes "Bundled ML weights for MyPhotos. Auto-downloaded by scripts/install-ml-models.sh — no HuggingFace token needed."
else
  echo "==> Release $TAG already exists in $REPO, will overwrite assets"
fi

# Upload, overwriting any existing asset with the same name.
# `gh release upload` accepts `<path>#<asset-name>` to rename on upload.
tmpdir="$(mktemp -d)"
trap 'rm -rf "$tmpdir"' EXIT

upload_args=()
for entry in "${ASSETS[@]}"; do
  rel="$(echo "$entry" | awk '{print $1}')"
  name="$(echo "$entry" | awk '{print $2}')"
  src="$MODELS_DIR/$rel"
  # gh upload uses basename of the file as the asset name. To rename,
  # symlink (or copy on Windows) into a temp dir under the desired name.
  ln -sf "$src" "$tmpdir/$name" 2>/dev/null || cp "$src" "$tmpdir/$name"
  upload_args+=("$tmpdir/$name")
done

echo "==> Uploading ${#upload_args[@]} assets to $REPO@$TAG"
gh release upload "$TAG" "${upload_args[@]}" \
  --repo "$REPO" \
  --clobber

echo
echo "==> Done. Public install:"
echo "    git clone https://github.com/$REPO && cd MyPhotos && ./scripts/install-ml-models.sh"

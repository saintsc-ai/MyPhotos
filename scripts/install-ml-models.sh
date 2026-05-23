#!/usr/bin/env bash
# Download ML model weights into data/models/.
#
# Public users (anyone who clones this repo) hit the project's own
# GitHub Release first — no auth, fast CDN. Maintainers seed that
# release once by downloading the upstream weights (which sometimes
# require a HuggingFace token) and attaching them via:
#     scripts/upload-ml-models.sh                  # uploads local data/models/ to the GH release
#     # …or manually via the GitHub Releases UI.
#
# Optional env vars
#   HF_TOKEN   — adds `Authorization: Bearer $HF_TOKEN` on huggingface.co
#                URLs. Only useful if you're seeding from HF directly.
#   MYPHOTOS_RELEASE_BASE
#              — override the GitHub release base URL (default points at
#                saintsc-ai/MyPhotos / models-v1). Fork-friendly.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MODELS_DIR="$ROOT/data/models"

# Where un-authenticated users pull from. Override via env when forking.
RELEASE_BASE="${MYPHOTOS_RELEASE_BASE:-https://github.com/saintsc-ai/MyPhotos/releases/download/models-v1}"

# --- helpers ----------------------------------------------------------------

fetch() {
  # fetch <primary_url> <fallback_url|""> <destfile> <min_bytes>
  local primary="$1" fallback="$2" dest="$3" min="$4"
  mkdir -p "$(dirname "$dest")"
  if [ -f "$dest" ]; then
    local sz
    sz=$(stat -c%s "$dest" 2>/dev/null || stat -f%z "$dest")
    if [ "$sz" -ge "$min" ]; then
      echo "    already present ($sz bytes), skipping"
      return 0
    fi
  fi

  for url in "$primary" "$fallback"; do
    [ -z "$url" ] && continue
    local fetch_url="$url"
    local hf_args=()
    if [[ "$url" == *huggingface.co* ]]; then
      if [[ "$url" != *download=true* ]]; then
        fetch_url="${url}?download=true"
      fi
      if [ -n "${HF_TOKEN:-}" ]; then
        hf_args=(-H "Authorization: Bearer $HF_TOKEN")
      fi
    fi
    echo "    trying $url"
    if curl -L --fail --create-dirs \
        -A "Mozilla/5.0 (compatible; MyPhotos)" \
        -H "Accept: application/octet-stream, */*" \
        "${hf_args[@]}" \
        -o "$dest" "$fetch_url"; then
      local actual
      actual=$(stat -c%s "$dest" 2>/dev/null || stat -f%z "$dest")
      if [ "$actual" -ge "$min" ]; then
        echo "    downloaded ($actual bytes)"
        return 0
      fi
      echo "    !! looks truncated ($actual bytes), trying next source"
      rm -f "$dest"
    fi
  done

  rm -f "$dest"
  echo
  echo "    !! All sources failed for $dest"
  if [[ "$primary" == *huggingface.co* || "$fallback" == *huggingface.co* ]] \
       && [ -z "${HF_TOKEN:-}" ]; then
    echo "    The HuggingFace source may be gated (401)."
    echo "    Options:"
    echo "      1. Wait — maintainers usually mirror weights to the project's"
    echo "         GitHub release ($RELEASE_BASE)."
    echo "      2. Get a free token at https://huggingface.co/settings/tokens"
    echo "         and re-run:    HF_TOKEN=hf_xxx ./scripts/install-ml-models.sh"
    echo "      3. Download manually in a browser and drop the file at:"
    echo "             $dest"
  fi
  exit 1
}

echo "==> Installing ML models into $MODELS_DIR"
echo "    primary source: $RELEASE_BASE"

# --- YOLOv8n (object detection) --------------------------------------------
echo "  - YOLOv8n"
fetch \
  "$RELEASE_BASE/yolov8n.onnx" \
  "https://huggingface.co/Xenova/yolov8n/resolve/main/onnx/model.onnx" \
  "$MODELS_DIR/yolo/yolov8n.onnx" \
  8000000

# --- CLIP ViT-B/32 (image+text embeddings, quantized INT8) -----------------
echo "  - CLIP vision (quantized)"
fetch \
  "$RELEASE_BASE/clip_vision_quantized.onnx" \
  "https://huggingface.co/Xenova/clip-vit-base-patch32/resolve/main/onnx/vision_model_quantized.onnx" \
  "$MODELS_DIR/clip/vision_quantized.onnx" \
  20000000

echo "  - CLIP text (quantized)"
fetch \
  "$RELEASE_BASE/clip_text_quantized.onnx" \
  "https://huggingface.co/Xenova/clip-vit-base-patch32/resolve/main/onnx/text_model_quantized.onnx" \
  "$MODELS_DIR/clip/text_quantized.onnx" \
  20000000

echo "  - CLIP tokenizer"
fetch \
  "$RELEASE_BASE/clip_tokenizer.json" \
  "https://huggingface.co/Xenova/clip-vit-base-patch32/resolve/main/tokenizer.json" \
  "$MODELS_DIR/clip/tokenizer.json" \
  500000

# --- Face detection + recognition (OpenCV Zoo, raw GitHub — no auth) -------
echo "  - YuNet face detector"
fetch \
  "https://raw.githubusercontent.com/opencv/opencv_zoo/refs/heads/main/models/face_detection_yunet/face_detection_yunet_2023mar.onnx" \
  "" \
  "$MODELS_DIR/face/yunet.onnx" \
  300000

echo "  - SFace face embedder"
fetch \
  "https://github.com/opencv/opencv_zoo/raw/refs/heads/main/models/face_recognition_sface/face_recognition_sface_2021dec.onnx" \
  "" \
  "$MODELS_DIR/face/sface.onnx" \
  30000000

echo
echo "==> Done. Restart the ml worker:"
echo "    sudo systemctl restart myphotos-ml-worker"

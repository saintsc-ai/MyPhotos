#!/usr/bin/env bash
# Download ML model weights into data/models/.
#
# Run once per host. Rerunning is safe — each model gets a size sanity-check
# so half-downloads on flaky links don't silently break inference.
#
# Some HuggingFace model repos are gated and refuse anonymous downloads
# (401). Get a free token at https://huggingface.co/settings/tokens and
# re-run with:
#     HF_TOKEN=hf_xxxxx ./scripts/install-ml-models.sh
# The token is only used to add an `Authorization: Bearer` header on
# huggingface.co URLs; OpenCV Zoo (raw.githubusercontent.com) is fetched
# without it.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MODELS_DIR="$ROOT/data/models"

# --- helpers ----------------------------------------------------------------

fetch() {
  # fetch <url> <destfile> <min_bytes>
  local url="$1" dest="$2" min="$3"
  mkdir -p "$(dirname "$dest")"
  if [ -f "$dest" ]; then
    local sz
    sz=$(stat -c%s "$dest" 2>/dev/null || stat -f%z "$dest")
    if [ "$sz" -ge "$min" ]; then
      echo "    already present ($sz bytes), skipping"
      return 0
    fi
  fi
  # HF sometimes 401s without a real User-Agent and ?download=true.
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
  if ! curl -L --fail --create-dirs \
      -A "Mozilla/5.0 (compatible; MyPhotos)" \
      -H "Accept: application/octet-stream, */*" \
      "${hf_args[@]}" \
      -o "$dest" "$fetch_url"; then
    rm -f "$dest"
    if [[ "$url" == *huggingface.co* ]] && [ -z "${HF_TOKEN:-}" ]; then
      echo
      echo "    !! HuggingFace returned an error. This model is likely gated."
      echo "    Get a free token at https://huggingface.co/settings/tokens"
      echo "    and re-run:    HF_TOKEN=hf_xxx ./scripts/install-ml-models.sh"
    fi
    exit 1
  fi
  local actual
  actual=$(stat -c%s "$dest" 2>/dev/null || stat -f%z "$dest")
  if [ "$actual" -lt "$min" ]; then
    echo "    !! download looks truncated ($actual bytes) — please retry"
    rm -f "$dest"
    exit 1
  fi
  echo "    downloaded ($actual bytes)"
}

echo "==> Installing ML models into $MODELS_DIR"

# --- Round 1: YOLOv8 nano (object detection, 80 COCO classes) --------------
echo "  - YOLOv8n"
fetch \
  "https://huggingface.co/Xenova/yolov8n/resolve/main/onnx/model.onnx" \
  "$MODELS_DIR/yolo/yolov8n.onnx" \
  8000000

# --- Round 2: CLIP ViT-B/32 (image+text embeddings) ------------------------
# Quantized INT8 variants — ~5x smaller than FP32, runs much faster on CPU,
# accuracy drop is negligible for zero-shot category matching.
echo "  - CLIP ViT-B/32 vision encoder (quantized)"
fetch \
  "https://huggingface.co/Xenova/clip-vit-base-patch32/resolve/main/onnx/vision_model_quantized.onnx" \
  "$MODELS_DIR/clip/vision_quantized.onnx" \
  20000000

echo "  - CLIP ViT-B/32 text encoder (quantized)"
fetch \
  "https://huggingface.co/Xenova/clip-vit-base-patch32/resolve/main/onnx/text_model_quantized.onnx" \
  "$MODELS_DIR/clip/text_quantized.onnx" \
  20000000

echo "  - CLIP tokenizer"
fetch \
  "https://huggingface.co/Xenova/clip-vit-base-patch32/resolve/main/tokenizer.json" \
  "$MODELS_DIR/clip/tokenizer.json" \
  500000

# --- Round 3: Face detection + recognition ---------------------------------
# OpenCV Zoo models — small, MIT-style license, stable URLs.
echo "  - YuNet face detector (OpenCV)"
fetch \
  "https://raw.githubusercontent.com/opencv/opencv_zoo/refs/heads/main/models/face_detection_yunet/face_detection_yunet_2023mar.onnx" \
  "$MODELS_DIR/face/yunet.onnx" \
  300000

echo "  - SFace face embedder (OpenCV)"
fetch \
  "https://github.com/opencv/opencv_zoo/raw/refs/heads/main/models/face_recognition_sface/face_recognition_sface_2021dec.onnx" \
  "$MODELS_DIR/face/sface.onnx" \
  30000000

echo
echo "==> Done. Restart the ml worker:"
echo "    sudo systemctl restart myphotos-ml-worker"

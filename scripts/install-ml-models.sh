#!/usr/bin/env bash
# Download ML model weights into data/models/.
#
# Run once per host. Rerunning is safe — `curl --create-dirs -L` will
# overwrite existing files, but each model also gets a size sanity-check
# below so half-downloads on flaky links don't silently break inference.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MODELS_DIR="$ROOT/data/models"

YOLO_DIR="$MODELS_DIR/yolo"
YOLO_FILE="$YOLO_DIR/yolov8n.onnx"
# Xenova ports the official Ultralytics weights to ONNX — single source
# we can curl without an Ultralytics install. ~12 MB.
YOLO_URL="https://huggingface.co/Xenova/yolov8n/resolve/main/onnx/model.onnx"
YOLO_MIN_SIZE=8000000   # ~12 MB; anything under 8 MB is suspicious

echo "==> Installing ML models into $MODELS_DIR"
mkdir -p "$YOLO_DIR"

echo "  - YOLOv8n  ($YOLO_FILE)"
if [ -f "$YOLO_FILE" ] && [ "$(stat -c%s "$YOLO_FILE" 2>/dev/null || stat -f%z "$YOLO_FILE")" -ge "$YOLO_MIN_SIZE" ]; then
  echo "    already present, skipping"
else
  curl -L --fail --create-dirs -o "$YOLO_FILE" "$YOLO_URL"
  actual=$(stat -c%s "$YOLO_FILE" 2>/dev/null || stat -f%z "$YOLO_FILE")
  if [ "$actual" -lt "$YOLO_MIN_SIZE" ]; then
    echo "    !! download looks truncated ($actual bytes) — please retry"
    rm -f "$YOLO_FILE"
    exit 1
  fi
  echo "    downloaded ($actual bytes)"
fi

# CLIP / face models (Round 2 + 3) will land here too:
# CLIP_FILE="$MODELS_DIR/clip/clip-vit-b32-int8.onnx"
# SCRFD_FILE="$MODELS_DIR/face/scrfd_2.5g.onnx"
# ARCFACE_FILE="$MODELS_DIR/face/arcface_w600k_r50.onnx"

echo "==> Done. Restart the ml worker:"
echo "    sudo systemctl restart myphotos-ml-worker"

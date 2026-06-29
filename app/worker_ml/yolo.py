"""YOLOv8 ONNX inference helper.

Runs on CPU via onnxruntime. Input is a JPEG path (typically the 1024px
thumbnail). Output is a list of detections — each a class id, confidence,
and bounding box normalized to the ORIGINAL image (not the letterbox
canvas). The caller dedups labels itself if it needs a tag-style summary.

The model is loaded once and shared across worker threads (onnxruntime's
InferenceSession is thread-safe for concurrent Run calls).
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import numpy as np

from ..paths import DATA_DIR

log = logging.getLogger(__name__)

MODEL_PATH = DATA_DIR / "models" / "yolo" / "yolov8n.onnx"
INPUT_SIZE = 640
CONF_THRESHOLD = 0.40   # below this we discard
IOU_THRESHOLD = 0.45    # NMS overlap


@dataclass
class Detection:
    class_id: int
    confidence: float
    # Bbox in [0..1] normalized to ORIGINAL image dims, [x, y, w, h]
    # top-left + size form — matches PhotoFace.bbox_json so the
    # frontend overlay code can be a near-copy. Backed out of the
    # letterbox padding + scale inside detect().
    bbox: tuple[float, float, float, float]


_session = None
_session_lock = threading.Lock()


def _load_session():
    global _session
    with _session_lock:
        if _session is not None:
            return _session
        if not MODEL_PATH.exists():
            raise FileNotFoundError(
                f"YOLO model not found at {MODEL_PATH} — run "
                f"scripts/install-ml-models.sh first."
            )
        from ._ort import make_session
        _session = make_session(MODEL_PATH)
        log.info("YOLO model loaded: %s", MODEL_PATH)
    return _session


def _letterbox(img: np.ndarray, target: int = INPUT_SIZE):
    """Resize image preserving aspect ratio, pad to target x target.

    Returns (canvas, scale, pad_left, pad_top, orig_w, orig_h) so the
    caller can map detections back from letterbox-pixel coords to
    normalized original-image coords.
    """
    h, w = img.shape[:2]
    scale = min(target / h, target / w)
    new_h, new_w = int(round(h * scale)), int(round(w * scale))
    from PIL import Image as _PIL

    pil = _PIL.fromarray(img).resize((new_w, new_h), _PIL.BILINEAR)
    resized = np.asarray(pil)
    # Pad to square
    pad_top = (target - new_h) // 2
    pad_left = (target - new_w) // 2
    canvas = np.full((target, target, 3), 114, dtype=np.uint8)
    canvas[pad_top:pad_top + new_h, pad_left:pad_left + new_w] = resized
    return canvas, scale, pad_left, pad_top, w, h


def _nms(boxes: np.ndarray, scores: np.ndarray, iou_thresh: float) -> list[int]:
    """Greedy NMS — return indices to keep, sorted by score desc."""
    if len(boxes) == 0:
        return []
    order = scores.argsort()[::-1]
    keep: list[int] = []
    while order.size > 0:
        i = int(order[0])
        keep.append(i)
        if order.size == 1:
            break
        xx1 = np.maximum(boxes[i, 0], boxes[order[1:], 0])
        yy1 = np.maximum(boxes[i, 1], boxes[order[1:], 1])
        xx2 = np.minimum(boxes[i, 2], boxes[order[1:], 2])
        yy2 = np.minimum(boxes[i, 3], boxes[order[1:], 3])
        w = np.maximum(0.0, xx2 - xx1)
        h = np.maximum(0.0, yy2 - yy1)
        inter = w * h
        ai = (boxes[i, 2] - boxes[i, 0]) * (boxes[i, 3] - boxes[i, 1])
        aj = (boxes[order[1:], 2] - boxes[order[1:], 0]) * (
            boxes[order[1:], 3] - boxes[order[1:], 1]
        )
        iou = inter / (ai + aj - inter + 1e-6)
        order = order[1:][iou < iou_thresh]
    return keep


def detect(image_path: str) -> Optional[list[Detection]]:
    """Run YOLO on a JPEG and return per-instance detections above
    CONF_THRESHOLD with bboxes normalized to the original image.
    None if the model can't load.
    """
    try:
        sess = _load_session()
    except FileNotFoundError as e:
        log.warning("%s", e)
        return None

    from PIL import Image

    try:
        with Image.open(image_path) as im:
            im = im.convert("RGB")
            arr = np.asarray(im)
    except Exception as e:
        log.warning("YOLO: failed to open %s: %s", image_path, e)
        return []

    canvas, scale, pad_left, pad_top, orig_w, orig_h = _letterbox(arr, INPUT_SIZE)
    # HWC uint8 → CHW float32 normalized 0..1, add batch dim
    blob = (
        canvas.astype(np.float32).transpose(2, 0, 1)[None, ...] / 255.0
    )

    input_name = sess.get_inputs()[0].name
    outputs = sess.run(None, {input_name: blob})
    # YOLOv8 output shape: [1, 84, 8400] where 84 = 4 bbox + 80 classes.
    raw = outputs[0]
    if raw.ndim == 3 and raw.shape[1] == 84:
        raw = raw[0].T  # → [8400, 84]
    elif raw.ndim == 3 and raw.shape[2] == 84:
        raw = raw[0]
    else:
        log.warning("YOLO: unexpected output shape %s", raw.shape)
        return []

    class_scores = raw[:, 4:]                       # [N, 80]
    class_ids = class_scores.argmax(axis=1)
    confidences = class_scores.max(axis=1)
    keep_mask = confidences >= CONF_THRESHOLD
    if not keep_mask.any():
        return []

    boxes_cxcywh = raw[keep_mask, :4]
    confidences = confidences[keep_mask]
    class_ids = class_ids[keep_mask]
    # cxcywh (letterbox-pixel) → xyxy for NMS
    x1 = boxes_cxcywh[:, 0] - boxes_cxcywh[:, 2] / 2
    y1 = boxes_cxcywh[:, 1] - boxes_cxcywh[:, 3] / 2
    x2 = boxes_cxcywh[:, 0] + boxes_cxcywh[:, 2] / 2
    y2 = boxes_cxcywh[:, 1] + boxes_cxcywh[:, 3] / 2
    boxes = np.stack([x1, y1, x2, y2], axis=1)

    keep = _nms(boxes, confidences, IOU_THRESHOLD)
    detections: list[Detection] = []
    for i in keep:
        # Back out letterbox padding + scale → original-image pixel
        # coords, then normalize to [0..1] for storage. Clamp to the
        # image rectangle: YOLO occasionally predicts edge-touching
        # boxes that drift a pixel or two over.
        bx1 = max(0.0, (float(x1[i]) - pad_left) / scale)
        by1 = max(0.0, (float(y1[i]) - pad_top) / scale)
        bx2 = min(float(orig_w), (float(x2[i]) - pad_left) / scale)
        by2 = min(float(orig_h), (float(y2[i]) - pad_top) / scale)
        if bx2 <= bx1 or by2 <= by1:
            continue
        nx = bx1 / orig_w
        ny = by1 / orig_h
        nw = (bx2 - bx1) / orig_w
        nh = (by2 - by1) / orig_h
        detections.append(
            Detection(
                class_id=int(class_ids[i]),
                confidence=float(confidences[i]),
                bbox=(nx, ny, nw, nh),
            )
        )
    return detections

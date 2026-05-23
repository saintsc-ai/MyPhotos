"""YOLOv8 ONNX inference helper.

Runs on CPU via onnxruntime. Input is a JPEG path (typically the 1024px
thumbnail). Output is a list of detections — each a class id + confidence;
we don't keep bounding boxes for tag-style classification.

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
        import onnxruntime as ort

        # Keep CPU usage predictable — single-thread per session, scale via
        # multiple worker threads at the dispatcher level instead.
        opts = ort.SessionOptions()
        opts.intra_op_num_threads = 1
        opts.inter_op_num_threads = 1
        _session = ort.InferenceSession(
            str(MODEL_PATH),
            sess_options=opts,
            providers=["CPUExecutionProvider"],
        )
        log.info("YOLO model loaded: %s", MODEL_PATH)
    return _session


def _letterbox(img: np.ndarray, target: int = INPUT_SIZE):
    """Resize image preserving aspect ratio, pad to target x target."""
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
    return canvas


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
    """Run YOLO on a JPEG and return distinct (class_id, max_confidence)
    detections above CONF_THRESHOLD. None if the model can't load.
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

    canvas = _letterbox(arr, INPUT_SIZE)
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
    # cxcywh → xyxy for NMS
    x1 = boxes_cxcywh[:, 0] - boxes_cxcywh[:, 2] / 2
    y1 = boxes_cxcywh[:, 1] - boxes_cxcywh[:, 3] / 2
    x2 = boxes_cxcywh[:, 0] + boxes_cxcywh[:, 2] / 2
    y2 = boxes_cxcywh[:, 1] + boxes_cxcywh[:, 3] / 2
    boxes = np.stack([x1, y1, x2, y2], axis=1)

    keep = _nms(boxes, confidences, IOU_THRESHOLD)
    # Collapse per-class to its top-confidence detection — for tag-style
    # output we only care "this photo has a dog", not how many.
    best_per_class: dict[int, float] = {}
    for i in keep:
        cid = int(class_ids[i])
        c = float(confidences[i])
        if c > best_per_class.get(cid, 0.0):
            best_per_class[cid] = c
    return [Detection(class_id=cid, confidence=c) for cid, c in best_per_class.items()]

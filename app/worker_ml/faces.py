"""Face detection (YuNet) + embedding (SFace) + online clustering.

Both models are OpenCV Zoo ONNX exports — small, MIT-style license,
stable URLs (see scripts/install-ml-models.sh).

Detection: YuNet at 320×320 (sufficient for thumbnails). Output is a
list of (bbox, landmarks, score). We keep landmarks for the alignment
step that ArcFace-style embedders need.

Embedding: SFace outputs a 128-d vector. We L2-normalize for cosine
similarity. Stored as float16 in photo_faces.embedding (256 bytes).

Clustering is incremental + cheap: each new face is matched against
all existing FaceCluster centroids; if the closest exceeds a threshold
the face joins that cluster (centroid is updated as a rolling mean),
otherwise a new cluster is created. Better-quality clustering (DBSCAN
over the full set) is a future "recluster" admin button.
"""

from __future__ import annotations

import json
import logging
import threading
from pathlib import Path
from typing import Optional

import numpy as np

from ..paths import DATA_DIR

log = logging.getLogger(__name__)

MODEL_DIR = DATA_DIR / "models" / "face"
YUNET_MODEL = MODEL_DIR / "yunet.onnx"
SFACE_MODEL = MODEL_DIR / "sface.onnx"

DETECT_SIZE_DEFAULT = 320       # YuNet preferred input edge (used when model is dynamic)
FACE_CROP_SIZE = 112            # SFace expects 112x112
EMBEDDING_DIM = 128             # SFace output
DETECT_CONF_THRESH = 0.7        # YuNet score
DETECT_NMS_THRESH = 0.3
CLUSTER_SIM_THRESHOLD = 0.45    # cosine sim to join existing cluster
MAX_FACES_PER_PHOTO = 10        # safety cap

_detector = None
_embedder = None
_lock = threading.Lock()


_detector_size: int = DETECT_SIZE_DEFAULT


def _load_detector():
    global _detector, _detector_size
    with _lock:
        if _detector is not None:
            return _detector
        if not YUNET_MODEL.exists():
            raise FileNotFoundError(f"YuNet model missing: {YUNET_MODEL}")
        import onnxruntime as ort
        opts = ort.SessionOptions()
        opts.intra_op_num_threads = 1
        opts.inter_op_num_threads = 1
        _detector = ort.InferenceSession(
            str(YUNET_MODEL), sess_options=opts, providers=["CPUExecutionProvider"]
        )
        # Probe the model's input shape. Some Zoo exports are fixed at
        # 640×640, others are dynamic — use the fixed size when present,
        # fall back to the default otherwise.
        shape = _detector.get_inputs()[0].shape  # [N, C, H, W] or with strings for dynamic
        edge = DETECT_SIZE_DEFAULT
        if isinstance(shape[-1], int) and isinstance(shape[-2], int) and shape[-1] == shape[-2]:
            edge = shape[-1]
        _detector_size = edge
        log.info("YuNet face detector loaded (input %dx%d)", edge, edge)
        return _detector


def _load_embedder():
    global _embedder
    with _lock:
        if _embedder is not None:
            return _embedder
        if not SFACE_MODEL.exists():
            raise FileNotFoundError(f"SFace model missing: {SFACE_MODEL}")
        import onnxruntime as ort
        opts = ort.SessionOptions()
        opts.intra_op_num_threads = 1
        opts.inter_op_num_threads = 1
        _embedder = ort.InferenceSession(
            str(SFACE_MODEL), sess_options=opts, providers=["CPUExecutionProvider"]
        )
        log.info("SFace embedder loaded")
        return _embedder


# --- YuNet decoding helpers ------------------------------------------------

# OpenCV Zoo's YuNet 2023mar exports 12 outputs: {cls,obj,bbox,kps}_{8,16,32}.
# Each stride has its own feature map of shape (1, H*W, C):
#   cls/obj : C=1 (sigmoid-activated already)
#   bbox    : C=4 (cx_off, cy_off, w_log, h_log)
#   kps     : C=10 (5 landmark x/y offsets)
# Score per anchor = sqrt(cls * obj). Box: (cx,cy) = (grid + off) * stride;
# w,h = exp(off) * stride.

_STRIDES = (8, 16, 32)
_GRID_CACHE: dict[tuple[int, int], np.ndarray] = {}


def _grid_xy(fmap: int, stride: int) -> np.ndarray:
    """Per-anchor (x_idx, y_idx) grid for a fmap×fmap feature map at `stride`."""
    key = (fmap, stride)
    if key in _GRID_CACHE:
        return _GRID_CACHE[key]
    xs = np.arange(fmap, dtype=np.float32)
    ys = np.arange(fmap, dtype=np.float32)
    gy, gx = np.meshgrid(ys, xs, indexing="ij")
    arr = np.stack([gx.reshape(-1), gy.reshape(-1)], axis=1)
    _GRID_CACHE[key] = arr
    return arr


def _nms(boxes: np.ndarray, scores: np.ndarray, iou_thresh: float) -> list[int]:
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


def _detect_faces(image: np.ndarray) -> list[dict]:
    """Run YuNet on a `image` (uint8 BGR HxWx3). Returns list of detections
    with keys: bbox (xyxy in input coords), score, landmarks (5x2)."""
    sess = _load_detector()
    edge = _detector_size

    h, w = image.shape[:2]
    # Letterbox to edge × edge.
    scale = edge / max(h, w)
    new_w = int(round(w * scale))
    new_h = int(round(h * scale))
    from PIL import Image as _PIL
    pil = _PIL.fromarray(image[..., ::-1])  # BGR → RGB for PIL
    pil = pil.resize((new_w, new_h), _PIL.BILINEAR)
    resized = np.asarray(pil)[..., ::-1]  # back to BGR
    canvas = np.zeros((edge, edge, 3), dtype=np.uint8)
    canvas[:new_h, :new_w] = resized

    blob = canvas.astype(np.float32).transpose(2, 0, 1)[None, ...]
    output_names = [o.name for o in sess.get_outputs()]
    outputs = sess.run(None, {sess.get_inputs()[0].name: blob})
    # Normalize names: some exports prefix with "/" or extra path segments.
    # Key by trailing token (cls_8, obj_16, bbox_32, kps_8, …).
    out_by_name: dict[str, np.ndarray] = {}
    for name, arr in zip(output_names, outputs):
        tail = name.rsplit("/", 1)[-1]
        out_by_name[tail] = arr

    # Per-stride decoding for the OpenCV Zoo 2023mar 12-output format.
    all_boxes: list[np.ndarray] = []
    all_landmarks: list[np.ndarray] = []
    all_scores: list[np.ndarray] = []
    for s in _STRIDES:
        try:
            cls = out_by_name[f"cls_{s}"].reshape(-1)
            obj = out_by_name[f"obj_{s}"].reshape(-1)
            bbox = out_by_name[f"bbox_{s}"].reshape(-1, 4)
            kps = out_by_name[f"kps_{s}"].reshape(-1, 10)
        except KeyError:
            log.warning(
                "YuNet: missing stride-%d outputs (have %s) — unsupported variant",
                s, output_names,
            )
            return []
        score = np.sqrt(np.clip(cls * obj, 0.0, 1.0))
        mask = score >= DETECT_CONF_THRESH
        if not mask.any():
            continue
        fmap = edge // s
        grid = _grid_xy(fmap, s)
        if grid.shape[0] != cls.shape[0]:
            log.warning(
                "YuNet: grid/output mismatch at stride %d (%d vs %d)",
                s, grid.shape[0], cls.shape[0],
            )
            return []
        sel_score = score[mask]
        sel_bbox = bbox[mask]
        sel_kps = kps[mask]
        sel_grid = grid[mask]
        cx = (sel_grid[:, 0] + sel_bbox[:, 0]) * s
        cy = (sel_grid[:, 1] + sel_bbox[:, 1]) * s
        bw = np.exp(sel_bbox[:, 2]) * s
        bh = np.exp(sel_bbox[:, 3]) * s
        boxes = np.stack([cx - bw / 2, cy - bh / 2, cx + bw / 2, cy + bh / 2], axis=1)
        lms = sel_kps.reshape(-1, 5, 2)
        lms_x = (sel_grid[:, 0:1] + lms[..., 0]) * s
        lms_y = (sel_grid[:, 1:2] + lms[..., 1]) * s
        landmarks = np.stack([lms_x, lms_y], axis=-1)
        all_boxes.append(boxes)
        all_landmarks.append(landmarks)
        all_scores.append(sel_score)

    if not all_boxes:
        return []
    boxes = np.vstack(all_boxes)
    landmarks = np.vstack(all_landmarks)
    sel_scores = np.hstack(all_scores)

    keep = _nms(boxes, sel_scores, DETECT_NMS_THRESH)
    if not keep:
        return []
    keep = keep[:MAX_FACES_PER_PHOTO]

    # Scale boxes/landmarks back to the original image coords.
    inv = 1.0 / scale
    out = []
    for i in keep:
        bb = boxes[i].copy()
        bb[[0, 2]] *= inv
        bb[[1, 3]] *= inv
        lm = landmarks[i].copy()
        lm *= inv
        out.append({
            "bbox": bb.tolist(),
            "score": float(sel_scores[i]),
            "landmarks": lm.tolist(),
        })
    return out


# --- SFace embedding -------------------------------------------------------

def _align_face(image: np.ndarray, landmarks: list) -> np.ndarray:
    """Simple crop using bbox enclosing landmarks. (Full 5-point similarity
    transform is ideal but overkill for our use; SFace tolerates loose crops.)
    """
    lms = np.array(landmarks)
    x1, y1 = lms.min(axis=0)
    x2, y2 = lms.max(axis=0)
    # Add padding around the landmark box (which is just eyes/nose/mouth).
    w = x2 - x1
    h = y2 - y1
    pad_x = w * 0.8
    pad_y = h * 1.0
    x1 = max(0, int(x1 - pad_x))
    y1 = max(0, int(y1 - pad_y))
    x2 = min(image.shape[1], int(x2 + pad_x))
    y2 = min(image.shape[0], int(y2 + pad_y * 0.6))
    crop = image[y1:y2, x1:x2]
    if crop.size == 0:
        return np.zeros((FACE_CROP_SIZE, FACE_CROP_SIZE, 3), dtype=np.uint8)
    from PIL import Image as _PIL
    pil = _PIL.fromarray(crop[..., ::-1])
    pil = pil.resize((FACE_CROP_SIZE, FACE_CROP_SIZE), _PIL.BILINEAR)
    return np.asarray(pil)[..., ::-1]   # BGR for SFace


def _l2_normalize(v: np.ndarray) -> np.ndarray:
    n = np.linalg.norm(v)
    return v / max(n, 1e-12)


def _embed_face(face_bgr: np.ndarray) -> np.ndarray:
    sess = _load_embedder()
    # SFace expects float32 (1, 3, 112, 112), pixel range 0..255.
    blob = face_bgr.astype(np.float32).transpose(2, 0, 1)[None, ...]
    out = sess.run(None, {sess.get_inputs()[0].name: blob})
    emb = out[0].reshape(-1)
    return _l2_normalize(emb).astype(np.float32)


# --- public API ------------------------------------------------------------

def detect_and_embed(image_path: str) -> Optional[list[dict]]:
    """Detect faces in the photo and return [{bbox, score, embedding}, ...].
    Returns None when models aren't installed yet (caller should leave the
    job pending for a later retry)."""
    try:
        _load_detector()
        _load_embedder()
    except FileNotFoundError as e:
        log.warning("%s", e)
        return None

    from PIL import Image as _PIL
    try:
        with _PIL.open(image_path) as im:
            im = im.convert("RGB")
            rgb = np.asarray(im)
    except Exception as e:
        log.warning("face: cannot open %s: %s", image_path, e)
        return []
    bgr = rgb[..., ::-1].copy()

    detections = _detect_faces(bgr)
    if not detections:
        return []

    h, w = bgr.shape[:2]
    results = []
    for d in detections:
        face_img = _align_face(bgr, d["landmarks"])
        emb = _embed_face(face_img)
        bx = d["bbox"]
        # Normalize bbox to [0..1] for storage so we can render on any thumb size.
        bb_norm = [
            float(bx[0] / w), float(bx[1] / h),
            float((bx[2] - bx[0]) / w), float((bx[3] - bx[1]) / h),
        ]
        results.append({
            "bbox": bb_norm,
            "score": d["score"],
            "embedding": emb,
        })
    return results


# --- packing for DB --------------------------------------------------------

def pack_embedding(vec: np.ndarray) -> bytes:
    return vec.astype(np.float16).tobytes()


def unpack_embedding(b: bytes) -> np.ndarray:
    return np.frombuffer(b, dtype=np.float16).astype(np.float32)


def cosine(a: np.ndarray, b: np.ndarray) -> float:
    return float(np.dot(a, b))


# --- online clustering -----------------------------------------------------

def assign_or_create_cluster(
    db,
    embedding: np.ndarray,
    threshold: float = CLUSTER_SIM_THRESHOLD,
):
    """Find the best-matching existing FaceCluster for this embedding (above
    `threshold`) or create a fresh one. Updates the cluster's centroid
    (running mean) and increments face_count. Returns the cluster row.
    """
    from ..models import FaceCluster
    from sqlalchemy import select

    clusters = db.execute(select(FaceCluster)).scalars().all()
    best = None
    best_sim = -1.0
    for c in clusters:
        if c.centroid is None:
            continue
        c_vec = unpack_embedding(c.centroid)
        sim = cosine(embedding, c_vec)
        if sim > best_sim:
            best_sim = sim
            best = c

    if best is not None and best_sim >= threshold:
        # Rolling mean update.
        c_vec = unpack_embedding(best.centroid)
        n = max(best.face_count, 1)
        new = (c_vec * n + embedding) / (n + 1)
        new = _l2_normalize(new)
        best.centroid = pack_embedding(new)
        best.face_count = n + 1
        return best

    # New cluster.
    new_cluster = FaceCluster(
        label=None,
        centroid=pack_embedding(embedding),
        face_count=1,
    )
    db.add(new_cluster)
    db.flush()
    return new_cluster

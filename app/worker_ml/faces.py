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

DETECT_SIZE = 320               # YuNet input edge length
FACE_CROP_SIZE = 112            # SFace expects 112x112
EMBEDDING_DIM = 128             # SFace output
DETECT_CONF_THRESH = 0.7        # YuNet score
DETECT_NMS_THRESH = 0.3
CLUSTER_SIM_THRESHOLD = 0.45    # cosine sim to join existing cluster
MAX_FACES_PER_PHOTO = 10        # safety cap

_detector = None
_embedder = None
_lock = threading.Lock()


def _load_detector():
    global _detector
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
        log.info("YuNet face detector loaded")
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

# YuNet uses three feature pyramid levels at strides 8, 16, 32. For each
# anchor, the output gives bbox offsets + 5 landmarks + score. We follow
# the OpenCV Zoo demo's decoding logic.
_PRIORS_CACHE: dict[int, np.ndarray] = {}


def _make_priors(size: int) -> np.ndarray:
    """Anchor priors for a square input of edge `size`."""
    if size in _PRIORS_CACHE:
        return _PRIORS_CACHE[size]
    strides = [8, 16, 32]
    priors = []
    for stride in strides:
        fmap = size // stride
        for y in range(fmap):
            for x in range(fmap):
                cx = (x + 0.5) * stride
                cy = (y + 0.5) * stride
                priors.append([cx, cy, stride])
    arr = np.array(priors, dtype=np.float32)
    _PRIORS_CACHE[size] = arr
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

    h, w = image.shape[:2]
    # Letterbox to DETECT_SIZE x DETECT_SIZE.
    scale = DETECT_SIZE / max(h, w)
    new_w = int(round(w * scale))
    new_h = int(round(h * scale))
    from PIL import Image as _PIL
    pil = _PIL.fromarray(image[..., ::-1])  # BGR → RGB for PIL
    pil = pil.resize((new_w, new_h), _PIL.BILINEAR)
    resized = np.asarray(pil)[..., ::-1]  # back to BGR
    canvas = np.zeros((DETECT_SIZE, DETECT_SIZE, 3), dtype=np.uint8)
    canvas[:new_h, :new_w] = resized

    blob = canvas.astype(np.float32).transpose(2, 0, 1)[None, ...]
    outputs = sess.run(None, {sess.get_inputs()[0].name: blob})

    # YuNet outputs (in order): loc [N,14], conf [N,2], iou [N,1]
    # where loc = 4 bbox + 10 landmarks.
    # Some exports concatenate differently — fall back gracefully.
    try:
        if len(outputs) == 3:
            loc, conf, iou = outputs
        else:
            # Concatenated [N, 15] or similar — try to split.
            arr = outputs[0]
            if arr.shape[-1] == 17:
                loc = arr[..., :14]
                conf = arr[..., 14:16]
                iou = arr[..., 16:17]
            else:
                log.warning("YuNet: unexpected output shape %s", arr.shape)
                return []
    except Exception as e:
        log.warning("YuNet output unpack failed: %s", e)
        return []

    if loc.ndim == 3:
        loc, conf, iou = loc[0], conf[0], iou[0]

    priors = _make_priors(DETECT_SIZE)
    if priors.shape[0] != loc.shape[0]:
        log.warning(
            "YuNet: prior/loc mismatch (%d vs %d) — model variant change?",
            priors.shape[0], loc.shape[0],
        )
        return []

    cls_scores = conf[:, 1]
    iou_scores = iou[:, 0].clip(0, 1)
    scores = np.sqrt(cls_scores * iou_scores)
    mask = scores >= DETECT_CONF_THRESH
    if not mask.any():
        return []

    sel_loc = loc[mask]
    sel_priors = priors[mask]
    sel_scores = scores[mask]
    # bbox decode (cx, cy, w, h) deltas
    cx = sel_priors[:, 0] + sel_loc[:, 0] * sel_priors[:, 2]
    cy = sel_priors[:, 1] + sel_loc[:, 1] * sel_priors[:, 2]
    bw = sel_priors[:, 2] * np.exp(sel_loc[:, 2])
    bh = sel_priors[:, 2] * np.exp(sel_loc[:, 3])
    x1 = cx - bw / 2
    y1 = cy - bh / 2
    x2 = cx + bw / 2
    y2 = cy + bh / 2
    boxes = np.stack([x1, y1, x2, y2], axis=1)

    # landmarks: 5 x (x, y) decoded similarly using priors
    lms = sel_loc[:, 4:14].reshape(-1, 5, 2)
    lms_x = sel_priors[:, 0:1] + lms[..., 0] * sel_priors[:, 2:3]
    lms_y = sel_priors[:, 1:2] + lms[..., 1] * sel_priors[:, 2:3]
    landmarks = np.stack([lms_x, lms_y], axis=-1)

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

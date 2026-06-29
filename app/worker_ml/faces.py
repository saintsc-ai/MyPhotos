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
CLUSTER_MERGE_THRESHOLD = 0.55  # merge two clusters if centroid cosine ≥ this
MIN_FACE_W_FRAC = 0.04          # re-cluster: skip faces narrower than this (× image width)
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
        from ._ort import make_session
        _detector = make_session(YUNET_MODEL)
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
        from ._ort import make_session
        _embedder = make_session(SFACE_MODEL)
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

# SFace/ArcFace canonical 5-point template for a 112×112 crop, in YuNet
# landmark order (right eye, left eye, nose, right-mouth, left-mouth) —
# this is exactly what OpenCV FaceRecognizerSF.alignCrop uses, which is
# what the SFace ONNX expects. Proper alignment (vs a loose crop) keeps the
# same person's embeddings consistent across pose → far fewer split/merged
# clusters.
_SFACE_REF = np.array([
    [38.2946, 51.6963],
    [73.5318, 51.5014],
    [56.0252, 71.7366],
    [41.5493, 92.3655],
    [70.7299, 92.2041],
], dtype=np.float64)


def _umeyama_similarity(src: np.ndarray, dst: np.ndarray) -> np.ndarray:
    """2D similarity transform (uniform scale·rotation + translation, no
    reflection) mapping src→dst, via Umeyama. Returns a 2×3 affine."""
    src = np.asarray(src, dtype=np.float64)
    dst = np.asarray(dst, dtype=np.float64)
    n = src.shape[0]
    src_mean = src.mean(axis=0)
    dst_mean = dst.mean(axis=0)
    src_d = src - src_mean
    dst_d = dst - dst_mean
    cov = (dst_d.T @ src_d) / n
    U, S, Vt = np.linalg.svd(cov)
    d = np.ones(2)
    if np.linalg.det(U) * np.linalg.det(Vt) < 0:
        d[-1] = -1.0
    R = U @ np.diag(d) @ Vt
    var_src = (src_d ** 2).sum() / n
    scale = float((S * d).sum() / var_src) if var_src > 1e-12 else 1.0
    t = dst_mean - scale * (R @ src_mean)
    M = np.zeros((2, 3), dtype=np.float64)
    M[:2, :2] = scale * R
    M[:, 2] = t
    return M


def _align_face(image: np.ndarray, landmarks: list) -> np.ndarray:
    """5-point similarity-aligned 112×112 BGR crop for SFace. Falls back to
    the loose landmark crop if the transform is degenerate."""
    try:
        src = np.asarray(landmarks, dtype=np.float64)
        if src.shape != (5, 2):
            raise ValueError("expected 5 landmarks")
        M = _umeyama_similarity(src, _SFACE_REF)   # input → 112×112 template
        A = M[:2, :2]
        t = M[:, 2]
        A_inv = np.linalg.inv(A)                   # PIL wants output → input
        t_inv = -A_inv @ t
        from PIL import Image as _PIL
        pil = _PIL.fromarray(image[..., ::-1])     # BGR → RGB
        coeffs = (A_inv[0, 0], A_inv[0, 1], t_inv[0],
                  A_inv[1, 0], A_inv[1, 1], t_inv[1])
        out = pil.transform(
            (FACE_CROP_SIZE, FACE_CROP_SIZE), _PIL.AFFINE, coeffs,
            resample=_PIL.BILINEAR,
        )
        return np.asarray(out)[..., ::-1]          # RGB → BGR
    except Exception:
        return _align_face_loose(image, landmarks)


def _align_face_loose(image: np.ndarray, landmarks: list) -> np.ndarray:
    """Fallback: padded bbox crop enclosing the landmarks (legacy behaviour)."""
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
    # fp32 — fp16 quantisation added cosine noise that hurt clustering.
    return vec.astype(np.float32).tobytes()


def unpack_embedding(b: bytes) -> np.ndarray:
    # Back-compat: legacy rows are float16 (256 B for a 128-d vector); new
    # rows are float32 (512 B). Detect by length so a fp32 read of old fp16
    # bytes doesn't return garbage.
    if len(b) == EMBEDDING_DIM * 2:
        return np.frombuffer(b, dtype=np.float16).astype(np.float32)
    return np.frombuffer(b, dtype=np.float32).astype(np.float32)


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
        # Skip ghost clusters — face_count<=0 means every member was
        # merged into another cluster, but the row + centroid stayed
        # behind. Without this guard a new face that resembles the
        # ghost's old centroid keeps re-binding to it, resurrecting
        # whatever stale (often typo'd) label was on the merge
        # source. patch_cluster now deletes the source on merge, but
        # legacy installs still have ghosts in the table — exclude
        # them by face_count as a belt-and-suspenders measure.
        if (c.face_count or 0) <= 0:
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


# --- offline re-clustering -------------------------------------------------

def recluster_all(
    db,
    *,
    join_threshold: float = CLUSTER_SIM_THRESHOLD,
    merge_threshold: float = CLUSTER_MERGE_THRESHOLD,
    min_face_frac: float = MIN_FACE_W_FRAC,
) -> dict:
    """Rebuild every face cluster from scratch over all stored embeddings.

    Quality-ordered leader clustering (best faces seed clusters) with a
    true-mean centroid, then a centroid-merge pass — avoids the order
    dependence and centroid drift of the incremental online assignment.
    Tiny faces (< min_face_frac of image width) are left unassigned. User
    labels are inherited by majority vote of each new cluster's members.
    Returns {clusters, assigned, skipped}.
    """
    import json as _json
    from collections import Counter

    from sqlalchemy import delete, select, update

    from ..models import FaceCluster, Photo, PhotoFace

    rows = db.execute(
        select(
            PhotoFace.id, PhotoFace.embedding, PhotoFace.bbox_json,
            PhotoFace.confidence, FaceCluster.label,
        )
        .join(Photo, Photo.id == PhotoFace.photo_id)
        .outerjoin(FaceCluster, FaceCluster.id == PhotoFace.cluster_id)
        .where(Photo.status == "active")
    ).all()

    faces = []          # (face_id, unit_vec, conf, width_frac, old_label)
    skipped = 0
    for fid, emb, bbox_json, conf, old_label in rows:
        try:
            v = unpack_embedding(emb)
        except Exception:
            continue
        nrm = float(np.linalg.norm(v))
        if nrm < 1e-9:
            continue
        v = v / nrm
        try:
            w_frac = float(_json.loads(bbox_json)[2])   # [x, y, w, h] in [0..1]
        except Exception:
            w_frac = 1.0
        faces.append((fid, v, float(conf or 0.0), w_frac, old_label))

    eligible = [f for f in faces if f[3] >= min_face_frac]
    skipped = len(faces) - len(eligible)
    eligible.sort(key=lambda f: (f[2], f[3]), reverse=True)   # best first

    clusters = []   # {sum, n, mean, members[], labels[]}
    for fid, v, _conf, _wf, old_label in eligible:
        best_i, best_sim = -1, -1.0
        for i, c in enumerate(clusters):
            sim = float(np.dot(v, c["mean"]))
            if sim > best_sim:
                best_sim, best_i = sim, i
        if best_i >= 0 and best_sim >= join_threshold:
            c = clusters[best_i]
            c["sum"] += v
            c["n"] += 1
            m = c["sum"] / c["n"]
            c["mean"] = m / max(float(np.linalg.norm(m)), 1e-9)
            c["members"].append(fid)
            c["labels"].append(old_label)
        else:
            clusters.append({"sum": v.copy(), "n": 1, "mean": v.copy(),
                             "members": [fid], "labels": [old_label]})

    # Merge pass — union-find on cluster centroids ≥ merge_threshold.
    parent = list(range(len(clusters)))

    def _find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for i in range(len(clusters)):
        for j in range(i + 1, len(clusters)):
            if float(np.dot(clusters[i]["mean"], clusters[j]["mean"])) >= merge_threshold:
                parent[_find(i)] = _find(j)

    groups: dict[int, dict] = {}
    for i, c in enumerate(clusters):
        g = groups.setdefault(_find(i), {"sum": np.zeros_like(c["sum"]),
                                         "members": [], "labels": []})
        g["sum"] += c["sum"]
        g["members"].extend(c["members"])
        g["labels"].extend(c["labels"])

    # Persist with SHORT, batched transactions so we never hold the single
    # SQLite writer lock long enough to starve the OCR/ML workers ("database
    # is locked"). Plan: create the new clusters, reassign faces to them in
    # small committed chunks, NULL the gated-out faces, then drop the old
    # clusters last. Each step commits on its own → lock released between.
    old_cluster_ids = [r[0] for r in db.execute(select(FaceCluster.id)).all()]

    created = []   # (cluster_id, [member_face_id, ...])
    for g in groups.values():
        mean = g["sum"] / max(len(g["members"]), 1)
        mean = mean / max(float(np.linalg.norm(mean)), 1e-9)
        labs = [l for l in g["labels"] if l]
        label = Counter(labs).most_common(1)[0][0] if labs else None
        fc = FaceCluster(label=label,
                         centroid=pack_embedding(mean.astype(np.float32)),
                         face_count=len(g["members"]))
        db.add(fc)
        db.flush()
        created.append((fc.id, g["members"]))
    db.commit()

    assigned = 0
    for cid, members in created:
        for off in range(0, len(members), 500):
            chunk = members[off:off + 500]
            db.execute(update(PhotoFace).where(PhotoFace.id.in_(chunk))
                       .values(cluster_id=cid))
            db.commit()
            assigned += len(chunk)

    # Gated-out (tiny) faces → unassigned.
    assigned_set = {fid for _cid, mem in created for fid in mem}
    to_null = [f[0] for f in faces if f[0] not in assigned_set]
    for off in range(0, len(to_null), 500):
        db.execute(update(PhotoFace).where(PhotoFace.id.in_(to_null[off:off + 500]))
                   .values(cluster_id=None))
        db.commit()

    # Drop the previous clusters (faces no longer reference them).
    for off in range(0, len(old_cluster_ids), 500):
        db.execute(delete(FaceCluster).where(
            FaceCluster.id.in_(old_cluster_ids[off:off + 500])))
        db.commit()

    log.info("recluster: %d clusters, %d assigned, %d skipped (small)",
             len(groups), assigned, skipped)
    return {"clusters": len(groups), "assigned": assigned, "skipped": skipped}

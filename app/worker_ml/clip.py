"""CLIP ViT-B/32 inference (image + text encoders, quantized ONNX).

The image encoder maps each photo's 1024 thumbnail to a 512-d embedding
that we cache in `photo_embeddings`. The text encoder runs once per
category prompt (results cached in-process) so categories can be matched
against every photo via a single cosine-similarity dot product.

Both encoders use onnxruntime CPU. Sessions are loaded lazily and shared
across worker threads.
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Optional

import numpy as np

from ..paths import DATA_DIR

log = logging.getLogger(__name__)

MODEL_DIR = DATA_DIR / "models" / "clip"
VISION_MODEL = MODEL_DIR / "vision_quantized.onnx"
TEXT_MODEL = MODEL_DIR / "text_quantized.onnx"
TOKENIZER_FILE = MODEL_DIR / "tokenizer.json"

VISION_INPUT_SIZE = 224
EMBEDDING_DIM = 512
TEXT_CTX_LEN = 77      # CLIP's fixed context length

# Standard CLIP image normalization.
CLIP_MEAN = np.array([0.48145466, 0.4578275, 0.40821073], dtype=np.float32)
CLIP_STD = np.array([0.26862954, 0.26130258, 0.27577711], dtype=np.float32)


_vision_session = None
_text_session = None
_tokenizer = None
_lock = threading.Lock()


def _load_vision():
    global _vision_session
    with _lock:
        if _vision_session is not None:
            return _vision_session
        if not VISION_MODEL.exists():
            raise FileNotFoundError(f"CLIP vision model missing: {VISION_MODEL}")
        from ._ort import make_session
        _vision_session = make_session(VISION_MODEL)
        log.info("CLIP vision encoder loaded")
        return _vision_session


def _load_text():
    global _text_session
    with _lock:
        if _text_session is not None:
            return _text_session
        if not TEXT_MODEL.exists():
            raise FileNotFoundError(f"CLIP text model missing: {TEXT_MODEL}")
        from ._ort import make_session
        _text_session = make_session(TEXT_MODEL)
        log.info("CLIP text encoder loaded")
        return _text_session


def _load_tokenizer():
    global _tokenizer
    with _lock:
        if _tokenizer is not None:
            return _tokenizer
        if not TOKENIZER_FILE.exists():
            raise FileNotFoundError(f"CLIP tokenizer missing: {TOKENIZER_FILE}")
        from tokenizers import Tokenizer
        _tokenizer = Tokenizer.from_file(str(TOKENIZER_FILE))
        log.info("CLIP tokenizer loaded")
        return _tokenizer


# --- preprocessing --------------------------------------------------------

def _preprocess_image(image_path: str) -> np.ndarray:
    """Resize → center-crop → normalize. Returns shape (1, 3, 224, 224)."""
    from PIL import Image as _PIL

    with _PIL.open(image_path) as im:
        im = im.convert("RGB")
        # Resize short side to VISION_INPUT_SIZE, preserving aspect.
        w, h = im.size
        if w < h:
            new_w = VISION_INPUT_SIZE
            new_h = int(round(h * VISION_INPUT_SIZE / w))
        else:
            new_h = VISION_INPUT_SIZE
            new_w = int(round(w * VISION_INPUT_SIZE / h))
        im = im.resize((new_w, new_h), _PIL.BICUBIC)
        # Center crop.
        left = (new_w - VISION_INPUT_SIZE) // 2
        top = (new_h - VISION_INPUT_SIZE) // 2
        im = im.crop((left, top, left + VISION_INPUT_SIZE, top + VISION_INPUT_SIZE))
        arr = np.asarray(im, dtype=np.float32) / 255.0

    arr = (arr - CLIP_MEAN) / CLIP_STD            # HWC normalize
    arr = arr.transpose(2, 0, 1)[None, ...]       # → (1, 3, H, W)
    return arr.astype(np.float32)


# --- inference ------------------------------------------------------------

def _l2_normalize(v: np.ndarray, axis: int = -1) -> np.ndarray:
    n = np.linalg.norm(v, axis=axis, keepdims=True)
    return v / np.maximum(n, 1e-12)


def encode_image(image_path: str) -> Optional[np.ndarray]:
    """Return a normalized 512-d float32 embedding for the photo, or None
    if the model isn't available."""
    try:
        sess = _load_vision()
    except FileNotFoundError as e:
        log.warning("%s", e)
        return None
    try:
        x = _preprocess_image(image_path)
    except Exception as e:
        log.warning("CLIP: preprocess failed for %s: %s", image_path, e)
        return None

    out = sess.run(None, {sess.get_inputs()[0].name: x})
    emb = out[0]              # shape (1, 512)
    emb = emb.reshape(-1)
    return _l2_normalize(emb).astype(np.float32)


def encode_text(prompts: list[str]) -> Optional[np.ndarray]:
    """Return normalized embeddings for `prompts`, shape (N, 512)."""
    try:
        sess = _load_text()
        tok = _load_tokenizer()
    except FileNotFoundError as e:
        log.warning("%s", e)
        return None

    # Pad/truncate each prompt to TEXT_CTX_LEN.
    encs = tok.encode_batch(prompts)
    input_ids = np.zeros((len(prompts), TEXT_CTX_LEN), dtype=np.int64)
    attention_mask = np.zeros((len(prompts), TEXT_CTX_LEN), dtype=np.int64)
    for i, e in enumerate(encs):
        ids = e.ids[:TEXT_CTX_LEN]
        mask = e.attention_mask[:TEXT_CTX_LEN]
        input_ids[i, : len(ids)] = ids
        attention_mask[i, : len(mask)] = mask

    # Xenova text exports vary in which inputs they accept — some only
    # take input_ids, others add attention_mask and/or position_ids.
    # Build the feed dict from the model's declared inputs.
    input_names = {inp.name for inp in sess.get_inputs()}
    feed: dict[str, np.ndarray] = {"input_ids": input_ids}
    if "attention_mask" in input_names:
        feed["attention_mask"] = attention_mask
    if "position_ids" in input_names:
        feed["position_ids"] = np.tile(
            np.arange(TEXT_CTX_LEN, dtype=np.int64), (len(prompts), 1)
        )
    out = sess.run(None, feed)
    emb = out[0]              # (N, 512)
    return _l2_normalize(emb).astype(np.float32)


# --- helpers --------------------------------------------------------------

def pack_vector(vec: np.ndarray) -> bytes:
    """Store float16 in the DB — halves the bytes; cosine-similarity precision is fine."""
    return vec.astype(np.float16).tobytes()


def unpack_vector(b: bytes, dim: int = EMBEDDING_DIM) -> np.ndarray:
    return np.frombuffer(b, dtype=np.float16).astype(np.float32).reshape(dim)


def cosine_similarity(a: np.ndarray, b: np.ndarray) -> float:
    """Both vectors assumed L2-normalized — dot product == cosine."""
    return float(np.dot(a, b))

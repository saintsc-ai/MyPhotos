"""OCR text extraction via RapidOCR (onnxruntime) for search.

Runs on the photo thumbnail; the extracted text is stored in
photos.ocr_text and folded into the FTS search index. The engine is
lazy + process-shared, mirroring the CLIP / face ONNX sessions.

Korean + English: RapidOCR's bundled models cover Latin (+ Chinese). For
Korean recognition, point the [ocr] config at a Korean PP-OCR rec model
(ONNX) + its keys file — see docs. When those paths are unset/missing we
fall back to the bundled models (so English/number text still works).
"""
from __future__ import annotations

import logging
import os
import threading
from typing import Optional

from ..config import get_settings

log = logging.getLogger(__name__)

_engine = None          # cached RapidOCR instance
_engine_tried = False   # True once we've attempted (and possibly failed) to build it
_lock = threading.Lock()


def _build_engine():
    """Construct a RapidOCR engine, applying optional model-path overrides
    (e.g. a Korean rec model) only when the files actually exist."""
    from rapidocr_onnxruntime import RapidOCR  # type: ignore

    s = get_settings().ocr
    kwargs = {}
    for key, val in (
        ("det_model_path", s.det_model_path),
        ("rec_model_path", s.rec_model_path),
        ("rec_keys_path", s.rec_keys_path),
        ("cls_model_path", s.cls_model_path),
    ):
        if val and os.path.exists(val):
            kwargs[key] = val
    if not kwargs:
        return RapidOCR()
    try:
        eng = RapidOCR(**kwargs)
        log.info("RapidOCR using custom models: %s", sorted(kwargs))
        return eng
    except TypeError:
        # Older/newer RapidOCR may not accept these kwargs — fall back so
        # the feature still works with bundled models.
        log.warning("RapidOCR did not accept model kwargs %s; using bundled models",
                    sorted(kwargs))
        return RapidOCR()


def _get_engine():
    """Return the shared engine, or None when rapidocr_onnxruntime isn't
    installed / can't initialise (caller then leaves the job pending so it
    auto-resumes after the package is installed)."""
    global _engine, _engine_tried
    if _engine is not None:
        return _engine
    with _lock:
        if _engine is not None:
            return _engine
        if _engine_tried:
            return None
        _engine_tried = True
        try:
            _engine = _build_engine()
            log.info("RapidOCR engine ready")
        except Exception as e:  # ImportError, missing deps, etc.
            log.warning("OCR unavailable (install the 'ocr' extra?): %s", e)
            _engine = None
        return _engine


def available() -> bool:
    return _get_engine() is not None


def extract_text(src: str) -> Optional[str]:
    """OCR an image file and return its concatenated text.

    Returns:
      None — engine unavailable (caller should leave the job pending).
      ""   — ran fine but found no text above the confidence threshold.
      str  — space-joined recognised lines (capped at ocr.max_chars).

    Raises on a genuine per-image OCR error (corrupt thumb, etc.) so the
    caller can mark that one photo 'failed' rather than looping forever.
    """
    engine = _get_engine()
    if engine is None:
        return None
    s = get_settings().ocr
    result, _elapse = engine(src)   # may raise → propagate to caller
    if not result:
        return ""
    lines = []
    for item in result:
        # RapidOCR row shape: [box, text, score]
        try:
            txt = item[1]
            score = float(item[2])
        except (IndexError, ValueError, TypeError):
            continue
        if txt and score >= s.min_score:
            t = str(txt).strip()
            if t:
                lines.append(t)
    text = " ".join(lines)
    if s.max_chars and len(text) > s.max_chars:
        text = text[: s.max_chars]
    return text

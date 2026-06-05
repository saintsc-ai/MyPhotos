"""OCR text extraction (RapidOCR) for search.

Runs on the photo thumbnail; the extracted text is stored in
photos.ocr_text and folded into the FTS search index. The engine is
lazy + process-shared, mirroring the CLIP / face ONNX sessions.

Two backends, auto-detected (v3 preferred):

* **rapidocr (v3)** — `pip install rapidocr`. Multilingual; the Korean
  model is fetched automatically when `[ocr] lang = "korean"`. No manual
  model files. Result is a RapidOCROutput dataclass (.txts/.scores).
* **rapidocr_onnxruntime (v1)** — older package. Bundled models cover
  Latin/Chinese only; for Korean set `[ocr] rec_model_path` +
  `rec_keys_path` to a Korean rec model + dict. Result is (rows, elapse).

Engine-unavailable (neither installed / init error) → extract_text
returns None so the caller leaves the job pending (auto-resumes after
install). A per-image error propagates so just that photo is marked
failed.
"""
from __future__ import annotations

import logging
import os
import threading
from typing import Optional

from ..config import get_settings

log = logging.getLogger(__name__)

# (backend, engine): backend is "v3" | "v1"; engine is the RapidOCR instance.
_engine = None
_backend: Optional[str] = None
_engine_tried = False
_lock = threading.Lock()


def _lang_rec_enum(lang: str):
    """Map a lang string (e.g. "korean") to rapidocr v3's LangRec enum —
    v3 requires Rec.lang_type be the enum, not a bare string. Tries the
    member name (KOREAN) then the value (korean). None if not found."""
    try:
        from rapidocr import LangRec  # type: ignore
    except Exception:
        return None
    try:
        return LangRec[lang.upper()]
    except KeyError:
        pass
    try:
        return LangRec(lang.lower())
    except (ValueError, KeyError):
        return None


def _build_v3():
    from rapidocr import RapidOCR  # type: ignore

    lang = (get_settings().ocr.lang or "").strip()
    params = {}
    if lang:
        lt = _lang_rec_enum(lang)
        if lt is not None:
            params["Rec.lang_type"] = lt
        else:
            log.warning("OCR v3: lang %r not in LangRec; using default model", lang)
    eng = RapidOCR(params=params) if params else RapidOCR()
    log.info("OCR backend: rapidocr v3 (lang=%s)", lang or "default")
    return eng


def _build_v1():
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
    try:
        eng = RapidOCR(**kwargs) if kwargs else RapidOCR()
    except TypeError:
        log.warning("rapidocr_onnxruntime did not accept %s; using bundled models",
                    sorted(kwargs))
        eng = RapidOCR()
    log.info("OCR backend: rapidocr_onnxruntime v1 (custom models: %s)", sorted(kwargs))
    return eng


def _get_engine():
    """Return (backend, engine), or (None, None) when no OCR package is
    installed / it fails to initialise."""
    global _engine, _backend, _engine_tried
    if _engine is not None:
        return _backend, _engine
    with _lock:
        if _engine is not None:
            return _backend, _engine
        if _engine_tried:
            return None, None
        _engine_tried = True
        # Prefer v3 (auto-downloads multilingual incl. Korean).
        for backend, build in (("v3", _build_v3), ("v1", _build_v1)):
            try:
                _engine = build()
                _backend = backend
                return _backend, _engine
            except ImportError:
                continue
            except Exception as e:
                log.warning("OCR %s init failed: %s", backend, e)
                continue
        log.warning("OCR unavailable — install 'rapidocr' (v3, auto Korean) "
                    "or 'rapidocr_onnxruntime' (v1).")
        return None, None


def available() -> bool:
    return _get_engine()[1] is not None


def _pairs(backend, out):
    """Yield (text, score) from either backend's result shape."""
    if out is None:
        return
    if backend == "v3":
        txts = getattr(out, "txts", None) or ()
        scores = getattr(out, "scores", None) or ()
        for t, sc in zip(txts, scores):
            yield t, sc
    else:  # v1: (rows, elapse); rows = [[box, text, score], ...]
        rows = out[0] if isinstance(out, tuple) else out
        for item in (rows or []):
            try:
                yield item[1], item[2]
            except (IndexError, TypeError):
                continue


def extract_text(src: str) -> Optional[str]:
    """OCR an image file and return its concatenated text.

    None → engine unavailable (leave job pending). '' → ran, no text.
    str  → space-joined recognised lines (capped at ocr.max_chars).
    Raises on a genuine per-image OCR error.
    """
    backend, engine = _get_engine()
    if engine is None:
        return None
    s = get_settings().ocr
    out = engine(src)   # may raise → propagate
    lines = []
    for txt, score in _pairs(backend, out):
        try:
            ok = float(score) >= s.min_score
        except (TypeError, ValueError):
            ok = True
        if txt and ok:
            t = str(txt).strip()
            if t:
                lines.append(t)
    text = " ".join(lines)
    if s.max_chars and len(text) > s.max_chars:
        text = text[: s.max_chars]
    return text

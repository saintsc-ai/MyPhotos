"""Shared ONNX Runtime session factory.

Centralizes execution-provider + threading config so the *same* model code
runs CPU-only on the Synology (the default) and GPU-accelerated on a bulk
indexing machine — flip ``[ml].onnx_providers`` in config, no code change.

Providers are tried in order; onnxruntime falls back to the next when an op
is unsupported, so ``CPUExecutionProvider`` must stay last (we append it if
the config forgets). If the requested GPU provider isn't actually available
in the installed onnxruntime build, we log it and fall back to CPU rather
than crash the worker.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from ..config import get_settings

log = logging.getLogger(__name__)

# Env overrides let a launcher (the desktop app's GPU dropdown) pick the
# execution provider per-process without editing config/local.toml — handy
# when the same checkout runs CPU-only on the NAS but GPU on a bulk box.
ENV_PROVIDERS = "MYPHOTOS_ONNX_PROVIDERS"        # e.g. "DmlExecutionProvider,CPUExecutionProvider"
ENV_INTRA_THREADS = "MYPHOTOS_ONNX_INTRA_THREADS"

# The special token "auto" expands to the best GPU provider actually present
# in the installed onnxruntime build, falling back to CPU. Preference order
# (fastest first). CPU is always appended, so "auto" is safe as a default:
# on a plain-onnxruntime NAS only CPU is available, so it resolves to CPU.
AUTO_TOKEN = "auto"
_AUTO_PREFERENCE = (
    "CUDAExecutionProvider",       # NVIDIA, onnxruntime-gpu
    "DmlExecutionProvider",        # any DX12 GPU on Windows, onnxruntime-directml
    "ROCMExecutionProvider",       # AMD on Linux
    "OpenVINOExecutionProvider",   # Intel CPU/iGPU, onnxruntime-openvino
    "CoreMLExecutionProvider",     # Apple Silicon
)


def _resolve_providers(requested: list[str], available: set[str]) -> list[str]:
    """Expand the 'auto' token to the best available accelerator and always
    end with CPU. Dedups while preserving order."""
    out: list[str] = []
    for p in requested:
        if p == AUTO_TOKEN:
            best = next((c for c in _AUTO_PREFERENCE if c in available), None)
            if best:
                out.append(best)
        else:
            out.append(p)
    if "CPUExecutionProvider" not in out:
        out.append("CPUExecutionProvider")  # always-available fallback
    seen: set[str] = set()
    return [p for p in out if not (p in seen or seen.add(p))]


def make_session(model_path):
    """Build an onnxruntime InferenceSession honouring the env overrides
    above, else the [ml] provider + thread settings. Safe to call from the
    lazy per-model loaders."""
    import onnxruntime as ort

    ml = get_settings().ml
    opts = ort.SessionOptions()
    intra = os.environ.get(ENV_INTRA_THREADS)
    opts.intra_op_num_threads = max(1, int(intra) if intra else int(ml.onnx_intra_op_threads))
    opts.inter_op_num_threads = max(1, int(ml.onnx_inter_op_threads))

    env_providers = os.environ.get(ENV_PROVIDERS)
    if env_providers:
        requested = [p.strip() for p in env_providers.split(",") if p.strip()]
    else:
        requested = list(ml.onnx_providers)
    if not requested:
        requested = [AUTO_TOKEN]

    available = set(ort.get_available_providers())
    providers = _resolve_providers(requested, available)
    usable = [p for p in providers if p in available or p == "CPUExecutionProvider"]
    missing = [p for p in providers if p not in available]
    if missing:
        log.warning(
            "ONNX providers %s not available in this onnxruntime build "
            "(have %s); using %s",
            missing, sorted(available), usable,
        )

    try:
        sess = ort.InferenceSession(
            str(model_path), sess_options=opts, providers=usable
        )
    except Exception:
        log.warning(
            "ONNX session for %s failed with providers=%s; retrying CPU-only",
            model_path, usable, exc_info=True,
        )
        sess = ort.InferenceSession(
            str(model_path), sess_options=opts,
            providers=["CPUExecutionProvider"],
        )
    log.info("ONNX session %s providers=%s", Path(model_path).name,
             sess.get_providers())
    return sess

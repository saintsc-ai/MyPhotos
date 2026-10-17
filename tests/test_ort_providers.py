"""ONNX Runtime session factory: CUDA provider options, VRAM cap, and the
"CUDA requested but not attached" warning; OCR's CUDA/version params."""

from __future__ import annotations

import logging
import sys
import types

import pytest

from app.worker_ml import _ort


@pytest.fixture
def fake_ort(monkeypatch):
    """Stand-in onnxruntime: records the providers each session was asked
    for and reports `attached` as what it actually got."""
    calls: list = []
    mod = types.ModuleType("onnxruntime")
    mod.available = ["CUDAExecutionProvider", "CPUExecutionProvider"]
    mod.attached = None  # None → echo the requested provider names

    class SessionOptions:
        intra_op_num_threads = 0
        inter_op_num_threads = 0

    class InferenceSession:
        def __init__(self, path, sess_options=None, providers=None):
            calls.append(providers)
            names = [p[0] if isinstance(p, tuple) else p for p in providers]
            self._got = mod.attached if mod.attached is not None else names

        def get_providers(self):
            return self._got

    mod.SessionOptions = SessionOptions
    mod.InferenceSession = InferenceSession
    mod.get_available_providers = lambda: list(mod.available)
    monkeypatch.setitem(sys.modules, "onnxruntime", mod)
    monkeypatch.setenv(_ort.ENV_PROVIDERS, "CUDAExecutionProvider,CPUExecutionProvider")
    monkeypatch.delenv(_ort.ENV_CUDA_MEM_LIMIT, raising=False)
    mod.calls = calls
    return mod


def test_cuda_options_default_has_no_mem_cap(monkeypatch):
    monkeypatch.delenv(_ort.ENV_CUDA_MEM_LIMIT, raising=False)
    opts = _ort.cuda_provider_options()
    assert opts["device_id"] == "0"
    assert opts["cudnn_conv_algo_search"] == "HEURISTIC"
    assert "gpu_mem_limit" not in opts


def test_cuda_options_env_mem_cap(monkeypatch):
    monkeypatch.setenv(_ort.ENV_CUDA_MEM_LIMIT, "1024")
    opts = _ort.cuda_provider_options()
    assert opts["gpu_mem_limit"] == str(1024 * 1024 * 1024)
    assert opts["arena_extend_strategy"] == "kSameAsRequested"


def test_make_session_passes_cuda_options(fake_ort, monkeypatch):
    monkeypatch.setenv(_ort.ENV_CUDA_MEM_LIMIT, "512")
    sess = _ort.make_session("model.onnx")
    (providers,) = fake_ort.calls
    assert providers[0][0] == "CUDAExecutionProvider"
    assert providers[0][1]["gpu_mem_limit"] == str(512 * 1024 * 1024)
    assert providers[1] == "CPUExecutionProvider"
    assert sess.get_providers()[0] == "CUDAExecutionProvider"


def test_make_session_warns_when_cuda_silently_dropped(fake_ort, caplog):
    fake_ort.attached = ["CPUExecutionProvider"]   # libs missing / no GPU
    with caplog.at_level(logging.WARNING, logger=_ort.__name__):
        _ort.make_session("model.onnx")
    assert any("CUDA requested but not attached" in r.getMessage() for r in caplog.records)


def test_cpu_only_build_falls_back_without_cuda_options(fake_ort):
    fake_ort.available = ["CPUExecutionProvider"]
    _ort.make_session("model.onnx")
    assert fake_ort.calls[-1] == ["CPUExecutionProvider"]


def test_ocr_cuda_params_follow_provider_resolution(monkeypatch):
    from app.worker_ml import ocr

    monkeypatch.setattr(_ort, "usable_providers", lambda: ["CPUExecutionProvider"])
    assert ocr._cuda_params() == {}

    monkeypatch.setattr(
        _ort, "usable_providers",
        lambda: ["CUDAExecutionProvider", "CPUExecutionProvider"],
    )
    monkeypatch.setenv(_ort.ENV_CUDA_MEM_LIMIT, "256")
    p = ocr._cuda_params()
    assert p["EngineConfig.onnxruntime.use_cuda"] is True
    assert p["EngineConfig.onnxruntime.cuda_ep_cfg.device_id"] == 0
    assert p["EngineConfig.onnxruntime.cuda_ep_cfg.gpu_mem_limit"] == 256 * 1024 * 1024


def test_ocr_version_params():
    pytest.importorskip("rapidocr")
    from app.worker_ml import ocr

    assert ocr._version_params("") == {}
    p = ocr._version_params("PP-OCRv4")
    assert p["Rec.ocr_version"].value == "PP-OCRv4"
    assert p["Det.model_type"].value == "mobile"
    assert ocr._version_params("PP-OCRv99") == {}

"""Unit tests for the unified per-photo ML job (run_classify_ml).

The image (photo) is the key; its status columns are the per-stage flags.
run_classify_ml runs only the stages still pending and skips the done ones.
The individual stage handlers are stubbed so we test the dispatch logic, not
the models.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.worker_ml import jobs as mj
from tests.conftest import make_photo, make_root


def _stub_stages(monkeypatch, calls):
    monkeypatch.setattr(mj, "run_classify_objects", lambda d, p: calls.append("obj"))
    monkeypatch.setattr(mj, "run_classify_embedding", lambda d, p: calls.append("emb"))
    monkeypatch.setattr(mj, "run_detect_faces", lambda d, p: calls.append("face"))
    monkeypatch.setattr(mj, "run_ocr_text", lambda d, p: calls.append("ocr"))


def test_run_classify_ml_runs_all_pending(db: Session, monkeypatch):
    calls: list[str] = []
    _stub_stages(monkeypatch, calls)
    root = make_root(db)
    p = make_photo(db, root, rel_path="a.jpg")
    p.sha256 = "a" * 64
    p.classify_status = "pending"
    p.ocr_status = None
    db.commit()

    mj.run_classify_ml(db, {"photo_id": p.id})
    assert calls == ["obj", "emb", "face", "ocr"]


def test_run_classify_ml_skips_done(db: Session, monkeypatch):
    calls: list[str] = []
    _stub_stages(monkeypatch, calls)
    root = make_root(db)
    p = make_photo(db, root, rel_path="b.jpg")
    p.sha256 = "b" * 64
    p.classify_status = "ok"
    p.ocr_status = "ok"
    db.commit()

    mj.run_classify_ml(db, {"photo_id": p.id})
    assert calls == []                       # nothing pending → no-op


def test_run_classify_ml_video_has_no_ocr(db: Session, monkeypatch):
    calls: list[str] = []
    _stub_stages(monkeypatch, calls)
    root = make_root(db)
    pv = make_photo(db, root, rel_path="v.mp4", media_kind="video", ext="mp4")
    pv.sha256 = "c" * 64
    pv.classify_status = "pending"
    pv.ocr_status = None
    db.commit()

    mj.run_classify_ml(db, {"photo_id": pv.id})
    assert calls == ["obj", "emb", "face"]   # classify runs, OCR skipped for video


def test_run_classify_ml_only_ocr_pending(db: Session, monkeypatch):
    calls: list[str] = []
    _stub_stages(monkeypatch, calls)
    root = make_root(db)
    p = make_photo(db, root, rel_path="d.jpg")
    p.sha256 = "d" * 64
    p.classify_status = "ok"      # classify done
    p.ocr_status = "pending"      # only OCR left
    db.commit()

    mj.run_classify_ml(db, {"photo_id": p.id})
    assert calls == ["ocr"]

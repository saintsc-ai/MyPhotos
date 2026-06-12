"""Unit tests for the unified per-photo ML job (run_classify_ml).

The image (photo) is the key; each ML stage has its own status column
(objects_status / clip_status / faces_status / ocr_status). run_classify_ml
runs only the stages still pending and skips the done ones. The individual
stage handlers are stubbed so we test the dispatch logic, not the models.
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


def _photo(db, root, *, rel, objects, clip, faces, ocr, media="image"):
    p = make_photo(db, root, rel_path=rel, media_kind=media,
                   ext="mp4" if media == "video" else "jpg")
    p.sha256 = (rel[:1] * 64)
    p.objects_status = objects
    p.clip_status = clip
    p.faces_status = faces
    p.ocr_status = ocr
    db.commit()
    return p


def test_run_classify_ml_runs_all_pending(db: Session, monkeypatch):
    calls: list[str] = []
    _stub_stages(monkeypatch, calls)
    root = make_root(db)
    p = _photo(db, root, rel="a.jpg", objects="pending", clip="pending",
               faces="pending", ocr=None)
    mj.run_classify_ml(db, {"photo_id": p.id})
    assert calls == ["obj", "emb", "face", "ocr"]


def test_run_classify_ml_skips_done(db: Session, monkeypatch):
    calls: list[str] = []
    _stub_stages(monkeypatch, calls)
    root = make_root(db)
    p = _photo(db, root, rel="b.jpg", objects="ok", clip="ok", faces="ok", ocr="ok")
    mj.run_classify_ml(db, {"photo_id": p.id})
    assert calls == []                       # nothing pending → no-op


def test_run_classify_ml_runs_only_pending_stage(db: Session, monkeypatch):
    """objects done, CLIP/faces pending, OCR done → only CLIP + faces run."""
    calls: list[str] = []
    _stub_stages(monkeypatch, calls)
    root = make_root(db)
    p = _photo(db, root, rel="c.jpg", objects="ok", clip="pending",
               faces="pending", ocr="ok")
    mj.run_classify_ml(db, {"photo_id": p.id})
    assert calls == ["emb", "face"]


def test_run_classify_ml_video_has_no_ocr(db: Session, monkeypatch):
    calls: list[str] = []
    _stub_stages(monkeypatch, calls)
    root = make_root(db)
    pv = _photo(db, root, rel="v.mp4", objects="pending", clip="pending",
                faces="pending", ocr=None, media="video")
    mj.run_classify_ml(db, {"photo_id": pv.id})
    assert calls == ["obj", "emb", "face"]   # classify runs, OCR skipped for video


def test_run_classify_ml_failed_stage_isolated(db: Session, monkeypatch):
    """A stage raising a non-lock error fails only its own column; the other
    stages still run + mark themselves ok, and classify_status rolls up to
    'failed'."""
    from app.models import Photo

    calls: list[str] = []

    def _ok(col, name):
        def f(d, payload):
            calls.append(name)
            ph = d.get(Photo, payload["photo_id"])
            setattr(ph, col, "ok")
            d.commit()
        return f

    def _boom(d, payload):
        calls.append("emb")
        raise ValueError("clip model exploded")

    monkeypatch.setattr(mj, "run_classify_objects", _ok("objects_status", "obj"))
    monkeypatch.setattr(mj, "run_classify_embedding", _boom)
    monkeypatch.setattr(mj, "run_detect_faces", _ok("faces_status", "face"))
    monkeypatch.setattr(mj, "run_ocr_text", _ok("ocr_status", "ocr"))

    root = make_root(db)
    p = _photo(db, root, rel="d.jpg", objects="pending", clip="pending",
               faces="pending", ocr=None)
    mj.run_classify_ml(db, {"photo_id": p.id})
    # CLIP raised, but objects/faces/ocr still ran.
    assert calls == ["obj", "emb", "face", "ocr"]
    db.refresh(p)
    assert p.objects_status == "ok"
    assert p.clip_status == "failed"         # only the failing stage
    assert p.faces_status == "ok"
    assert p.classify_status == "failed"     # roll-up: failed + none pending

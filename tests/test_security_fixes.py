"""Regression tests for the 2026-10 internet-exposure security review.

Each test pins one fix so a later refactor can't quietly reopen it:
path canonicalisation (root wipe via "." / ACL dodge via ".."), control
characters in names (exiftool argfile injection), visibility in the bulk
ACL check, and X-Forwarded-For only behind a declared proxy.
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException
from sqlalchemy.orm import Session
from starlette.testclient import TestClient

from app.scanner.utils import clean_rel_path, has_control_chars
from tests.conftest import grant_root, make_photo, make_root, make_user


# ----- clean_rel_path -----

@pytest.mark.parametrize("raw,expected", [
    ("", ""),
    ("/", ""),
    ("2024/01", "2024/01"),
    ("/2024/01/", "2024/01"),
    ("가족/사진", "가족/사진"),
])
def test_clean_rel_path_accepts_plain_paths(raw, expected):
    assert clean_rel_path(raw) == expected


@pytest.mark.parametrize("raw", [
    ".", "./", "a/..", "a/../b", "../etc", "a//b", "a\\..\\b",
    "C:", "C:/Windows", "x\ny", "x\ry", "x\x00y",
])
def test_clean_rel_path_rejects_traversal_and_control(raw):
    with pytest.raises(ValueError):
        clean_rel_path(raw)


def test_has_control_chars():
    assert has_control_chars("a\nb")
    assert has_control_chars("a\x7fb")
    assert not has_control_chars("평범한 이름.jpg")


# ----- folder routes -----

def _writable_root(db: Session, tmp_path):
    root = make_root(db, abs_path=str(tmp_path))
    (tmp_path / "keep.jpg").write_bytes(b"x")
    (tmp_path / "sub").mkdir()
    db.commit()
    return root


@pytest.mark.parametrize("rel", [".", "./", "sub/..", "sub/../"])
def test_delete_folder_refuses_root_aliases(client: TestClient, db: Session, tmp_path, rel):
    root = _writable_root(db, tmp_path)
    r = client.request("DELETE", "/api/admin/folders",
                       json={"root_id": root.id, "rel_path": rel, "recursive": False})
    assert r.status_code == 400
    assert (tmp_path / "keep.jpg").exists()


def test_create_folder_refuses_newline_name(client: TestClient, db: Session, tmp_path):
    root = _writable_root(db, tmp_path)
    r = client.post("/api/admin/folders",
                    json={"root_id": root.id, "parent_rel_path": "",
                          "name": "x\n-if\n1"})
    assert r.status_code == 400
    assert not any("\n" in p.name for p in tmp_path.iterdir())


def test_list_folders_refuses_absolute_prefix(client: TestClient, db: Session, tmp_path):
    root = _writable_root(db, tmp_path)
    for prefix in ("/etc", "../..", "sub/../.."):
        r = client.get("/api/photos/folders",
                       params={"root_id": root.id, "prefix": prefix})
        # "/etc" strips to "etc" (a harmless, non-existent subfolder);
        # anything with ".." must be refused outright.
        if ".." in prefix:
            assert r.status_code == 400, prefix
        else:
            assert r.status_code in (200, 404), prefix


# ----- exiftool argfile -----

def test_exiftool_refuses_newline_args(monkeypatch):
    from app.worker import exiftool_pool

    def _boom():
        raise AssertionError("must not reach the exiftool process")

    monkeypatch.setattr(exiftool_pool, "_get", _boom)
    assert exiftool_pool._exec_text(["-j", "/photos/x\n-if\nsystem('id')"]) is None


# ----- bulk ACL honours visibility -----

def test_bulk_level_hides_others_private_photo(db: Session):
    from app.auth_acl import require_photo_ids_level

    owner = make_user(db, username="owner")
    other = make_user(db, username="other")
    root = make_root(db)
    p = make_photo(db, root, rel_path="p.jpg", owner_user_id=owner.id,
                   visibility="private")
    db.commit()

    with pytest.raises(HTTPException) as e:
        require_photo_ids_level(db, other, [p.id], "read")
    assert e.value.status_code == 404
    assert require_photo_ids_level(db, owner, [p.id], "read")[p.id] == "manage"


def test_bulk_level_public_floor(db: Session):
    from app.auth_acl import require_photo_ids_level

    u = make_user(db, username="u")
    root = make_root(db)
    grant_root(db, root, u, "hidden")
    p = make_photo(db, root, rel_path="pub.jpg", visibility="public")
    db.commit()
    assert require_photo_ids_level(db, u, [p.id], "read")[p.id] == "read"


# ----- X-Forwarded-For -----

class _Req:
    def __init__(self, xff: str | None, host: str = "203.0.113.9"):
        self.headers = {"x-forwarded-for": xff} if xff else {}
        self.client = type("C", (), {"host": host})()


def test_client_ip_ignores_xff_without_trusted_proxy(monkeypatch):
    from app import auth

    sec = auth.get_settings().security
    monkeypatch.setattr(sec, "trust_proxy_xff", False)
    assert auth._client_ip(_Req("1.2.3.4")) == "203.0.113.9"
    monkeypatch.setattr(sec, "trust_proxy_xff", True)
    assert auth._client_ip(_Req("1.2.3.4, 10.0.0.1")) == "1.2.3.4"


def test_password_policy():
    from app.auth import validate_new_password

    for bad in ("short", "admin", "ADMIN", "alice123", "가" * 25):
        with pytest.raises(HTTPException) as e:
            validate_new_password(bad, "alice123")
        assert e.value.status_code == 400, bad
    validate_new_password("correct horse", "alice123")


class _SessReq:
    def __init__(self):
        self.session: dict = {}


def test_password_change_invalidates_old_sessions(db: Session):
    from app.auth import current_user, hash_password, start_session

    u = make_user(db, username="s")
    u.password_hash = hash_password("first-password")
    db.commit()
    old = _SessReq()
    start_session(old, u)
    assert current_user(old, db).id == u.id

    u.password_hash = hash_password("second-password")
    db.commit()
    assert current_user(old, db) is None           # stolen/old cookie dead
    legacy = _SessReq()
    legacy.session["user_id"] = u.id                # pre-upgrade cookie
    assert current_user(legacy, db) is None


def test_seed_hash_check_is_memoised(monkeypatch):
    from app import auth

    h = auth.hash_password("admin")
    auth.is_seed_hash.cache_clear()
    calls = []
    real = auth.verify_password
    monkeypatch.setattr(auth, "verify_password", lambda p, x: calls.append(1) or real(p, x))
    assert auth.is_seed_hash(h) and auth.is_seed_hash(h)
    assert len(calls) == 1


def test_share_unlock_throttle_window():
    from app import shares

    shares._unlock_failures.clear()
    for _ in range(shares._UNLOCK_MAX_PER_IP):
        shares._unlock_record("t|1.2.3.4", 1000.0)
    assert shares._unlock_recent("t|1.2.3.4", 1001.0) >= shares._UNLOCK_MAX_PER_IP
    assert shares._unlock_recent("t|1.2.3.4", 1000.0 + shares._UNLOCK_WINDOW_S + 1) == 0


def test_share_password_min_length():
    from app.shares import _share_pw_hash

    assert _share_pw_hash(None) is None and _share_pw_hash("") is None
    with pytest.raises(HTTPException):
        _share_pw_hash("1234")
    assert _share_pw_hash("123456").startswith("$2")


def _share_with(db: Session, photo, **kw):
    from app.models import Share, ShareItem

    s = Share(token="tok" + str(photo.id), **kw)
    db.add(s)
    db.flush()
    db.add(ShareItem(share_id=s.id, photo_id=photo.id))
    db.commit()
    return s


def test_share_video_route_rejects_images(client: TestClient, db: Session, tmp_path):
    root = make_root(db, abs_path=str(tmp_path))
    (tmp_path / "a.jpg").write_bytes(b"\xff\xd8" + b"x" * 2000)
    p = make_photo(db, root, rel_path="a.jpg")
    s = _share_with(db, p, strip_exif=True, max_downloads=1)
    r = client.get(f"/api/share/{s.token}/video/{p.id}")
    assert r.status_code == 404


def test_share_strip_fails_closed_for_video(client: TestClient, db: Session, tmp_path):
    root = make_root(db, abs_path=str(tmp_path))
    (tmp_path / "v.mp4").write_bytes(b"\x00" * 4000)
    p = make_photo(db, root, rel_path="v.mp4", media_kind="video", ext="mp4")
    s = _share_with(db, p, strip_exif=True, max_downloads=1)
    r = client.get(f"/api/share/{s.token}/original/{p.id}")
    assert r.status_code == 409
    db.refresh(s)
    assert (s.download_count or 0) == 0             # refusal didn't burn the slot
    r = client.get(f"/api/share/{s.token}/video/{p.id}")
    assert r.status_code == 409                     # no raw original fallback


def test_trashed_photo_leaves_share(client: TestClient, db: Session, tmp_path):
    root = make_root(db, abs_path=str(tmp_path))
    p = make_photo(db, root, rel_path="gone.jpg", status="trashed")
    s = _share_with(db, p)
    assert client.get(f"/api/share/{s.token}/thumb/{p.id}").status_code == 404


def test_gzip_skips_binary_and_partial():
    from fastapi import FastAPI
    from fastapi.responses import Response
    from starlette.middleware.gzip import GZipMiddleware

    from app.api.main import _GzipSkipMark, _GzipSkipStrip

    a = FastAPI()
    a.add_middleware(_GzipSkipMark)
    a.add_middleware(GZipMiddleware, minimum_size=10)
    a.add_middleware(_GzipSkipStrip)

    @a.get("/json")
    def _j():
        return {"x": "y" * 5000}

    @a.get("/img")
    def _i():
        return Response(b"\x00" * 5000, media_type="image/jpeg")

    @a.get("/part")
    def _p():
        return Response(b"a" * 5000, status_code=206, media_type="text/plain")

    c = TestClient(a)
    h = {"Accept-Encoding": "gzip"}
    assert c.get("/json", headers=h).headers.get("content-encoding") == "gzip"
    for path in ("/img", "/part"):
        r = c.get(path, headers=h)
        assert "content-encoding" not in r.headers, path
        assert len(r.content) == 5000


def test_login_failure_map_is_bounded(monkeypatch):
    from app import auth

    monkeypatch.setattr(auth, "_LOGIN_FAIL_MAX_IPS", 50)
    monkeypatch.setattr(auth, "_login_failures", {})
    for i in range(500):
        auth._login_throttle_record_failure(f"10.0.{i // 256}.{i % 256}")
    assert len(auth._login_failures) <= 50

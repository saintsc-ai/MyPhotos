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


def test_login_failure_map_is_bounded(monkeypatch):
    from app import auth

    monkeypatch.setattr(auth, "_LOGIN_FAIL_MAX_IPS", 50)
    monkeypatch.setattr(auth, "_login_failures", {})
    for i in range(500):
        auth._login_throttle_record_failure(f"10.0.{i // 256}.{i % 256}")
    assert len(auth._login_failures) <= 50

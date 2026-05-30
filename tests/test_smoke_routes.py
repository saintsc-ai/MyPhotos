"""Phase 1 smoke tests for the HTTP layer.

Goal: exercise every router with one or two requests so a refactor
that breaks routing, dependency wiring, response models or status
codes shows up in CI immediately. Not a substitute for behavior
tests — just a tripwire.

All requests run as a fixed admin user via the conftest `client`
fixture, so the tests bypass per-flag auth and focus on the route
itself responding with the expected shape.
"""

from __future__ import annotations

from sqlalchemy.orm import Session
from starlette.testclient import TestClient

from tests.conftest import make_photo, make_root


# ====================================================================
# /api/photos
# ====================================================================


def test_photos_list_empty(client: TestClient):
    r = client.get("/api/photos?page=1&page_size=10")
    assert r.status_code == 200
    data = r.json()
    assert data["page"] == 1
    assert data["page_size"] == 10
    assert data["items"] == []


def test_photos_list_returns_seeded_rows(client: TestClient, db: Session):
    root = make_root(db, label="test", abs_path="/tmp/test")
    make_photo(db, root, rel_path="a.jpg")
    make_photo(db, root, rel_path="sub/b.jpg")
    db.commit()
    r = client.get("/api/photos?page=1&page_size=10")
    assert r.status_code == 200
    data = r.json()
    assert data["total"] == 2
    assert len(data["items"]) == 2
    # Response shape we care about for the gallery client.
    item = data["items"][0]
    for key in ("id", "rel_path", "filename", "media_kind"):
        assert key in item


def test_photos_detail_404_when_missing(client: TestClient):
    r = client.get("/api/photos/999999")
    assert r.status_code == 404


def test_photos_detail_ok(client: TestClient, db: Session):
    root = make_root(db)
    p = make_photo(db, root, rel_path="x.jpg")
    db.commit()
    r = client.get(f"/api/photos/{p.id}")
    assert r.status_code == 200
    assert r.json()["id"] == p.id


def test_photos_date_histogram_shape(client: TestClient):
    r = client.get("/api/photos/date-histogram")
    assert r.status_code == 200
    assert isinstance(r.json(), list)


def test_photos_bulk_rotate_400_on_empty_list(client: TestClient):
    r = client.post("/api/photos/bulk-rotate",
                    json={"photo_ids": [], "direction": "cw"})
    assert r.status_code == 400


def test_photos_bulk_delete_400_on_empty_list(client: TestClient):
    r = client.post("/api/photos/bulk-delete", json={"photo_ids": []})
    assert r.status_code == 400


def test_photos_set_gps_round_trip(
    client: TestClient, db: Session, tmp_path, monkeypatch,
):
    """PUT /gps writes the GPS tags into the file's EXIF (admin-only)
    and mirrors them into the photo_locations table. Source of truth
    is the file. We swap in a stub for the ExifTool call so the test
    doesn't depend on the binary being installed — what we're checking
    here is the endpoint contract (auth → writability → DB op shape),
    not ExifTool itself. (worker.exif_write has its own unit tests
    against real files.)
    """
    from app.models import PhotoLocation
    from app.worker import exif_write
    from app.api import routes_photos

    # 1) Real file on disk inside a per-test root so check_write_blocked
    #    has something to stat.
    photo_file = tmp_path / "gps.jpg"
    photo_file.write_bytes(b"\xff\xd8\xff\xd9")   # 4-byte JPEG SOI/EOI

    # 2) Stub exiftool_path() so the endpoint doesn't bail with 503,
    #    and stub the three write helpers so the test doesn't need
    #    the binary. Validation that the helpers themselves work
    #    against a real file is the job of test_worker_exif_write.
    monkeypatch.setattr(routes_photos, "exiftool_path", lambda: "/fake/exiftool")
    monkeypatch.setattr(
        exif_write, "write_gps",
        lambda tool, p, lat, lng, alt: exif_write.ExifWriteResult(ok=True),
    )
    monkeypatch.setattr(
        exif_write, "clear_gps",
        lambda tool, p: exif_write.ExifWriteResult(ok=True),
    )

    root = make_root(db, abs_path=str(tmp_path))
    p = make_photo(db, root, rel_path="gps.jpg")
    db.commit()

    # First set: insert.
    r = client.put(f"/api/photos/{p.id}/gps",
                   json={"latitude": 37.5, "longitude": 127.0, "altitude": None})
    assert r.status_code == 200, r.text
    assert r.json()["latitude"] == 37.5
    db.expire_all()
    assert db.get(PhotoLocation, p.id) is not None

    # Update: same row, new coords.
    r = client.put(f"/api/photos/{p.id}/gps",
                   json={"latitude": 35.0, "longitude": 129.0, "altitude": 50.0})
    assert r.status_code == 200
    db.expire_all()
    loc = db.get(PhotoLocation, p.id)
    assert loc.latitude == 35.0 and loc.altitude == 50.0

    # Clear: nulls remove the row.
    r = client.put(f"/api/photos/{p.id}/gps",
                   json={"latitude": None, "longitude": None, "altitude": None})
    assert r.status_code == 200
    db.expire_all()
    assert db.get(PhotoLocation, p.id) is None

    # Out-of-range latitude is rejected by Pydantic (422).
    r = client.put(f"/api/photos/{p.id}/gps",
                   json={"latitude": 99.0, "longitude": 0.0})
    assert r.status_code == 422

    # Half-set (lat without lng) is rejected at the handler level (400).
    r = client.put(f"/api/photos/{p.id}/gps",
                   json={"latitude": 37.0, "longitude": None})
    assert r.status_code == 400


def test_photos_set_gps_409_when_root_readonly(
    client: TestClient, db: Session, tmp_path, monkeypatch,
):
    """Even with admin auth, GPS edit is refused (409) when the root
    is readonly — file-write endpoints honour the same gate that
    rotate / trash use, so the user gets a clear "read-only mode"
    message instead of a generic 500 from the file layer."""
    from app.api import routes_photos
    monkeypatch.setattr(routes_photos, "exiftool_path", lambda: "/fake/exiftool")
    (tmp_path / "ro.jpg").write_bytes(b"\xff\xd8\xff\xd9")
    root = make_root(db, abs_path=str(tmp_path))
    root.readonly = True
    p = make_photo(db, root, rel_path="ro.jpg")
    db.commit()
    r = client.put(f"/api/photos/{p.id}/gps",
                   json={"latitude": 37.5, "longitude": 127.0})
    assert r.status_code == 409
    assert "읽기 전용" in r.json()["detail"]


# ====================================================================
# /api/admin/* — one smoke per router
# ====================================================================


def test_admin_roots_list(client: TestClient, db: Session):
    make_root(db, label="r1", abs_path="/tmp/r1")
    db.commit()
    r = client.get("/api/admin/roots")
    assert r.status_code == 200
    assert any(row["label"] == "r1" for row in r.json())


def test_admin_jobs_stats(client: TestClient):
    r = client.get("/api/admin/jobs/stats")
    assert r.status_code == 200


def test_admin_audit_list(client: TestClient):
    r = client.get("/api/admin/audit?page=1&page_size=10")
    assert r.status_code == 200
    body = r.json()
    assert "items" in body and "total" in body


def test_admin_database_info(client: TestClient):
    r = client.get("/api/admin/database/info")
    assert r.status_code == 200


def test_admin_duplicates_stats(client: TestClient):
    r = client.get("/api/admin/duplicates/stats")
    assert r.status_code == 200


def test_admin_trash_list_empty(client: TestClient):
    r = client.get("/api/admin/trash?page=1&page_size=10")
    assert r.status_code == 200
    assert r.json()["items"] == []


def test_admin_settings_get(client: TestClient):
    r = client.get("/api/admin/settings")
    assert r.status_code == 200


def test_admin_ml_stats(client: TestClient):
    r = client.get("/api/admin/ml/stats")
    assert r.status_code == 200


def test_admin_users_list(client: TestClient):
    r = client.get("/api/admin/users")
    assert r.status_code == 200
    # At least the testadmin we seeded via the fixture.
    assert any(u["username"] == "testadmin" for u in r.json())


def test_admin_folders_acl_empty(client: TestClient, db: Session):
    root = make_root(db, label="folders-test", abs_path="/tmp/ft")
    db.commit()
    r = client.get(f"/api/admin/folders/{root.id}/acl")
    assert r.status_code == 200
    assert r.json() == []


# ====================================================================
# /api/shares — full create → list → revoke round-trip
# ====================================================================


def test_shares_round_trip(client: TestClient, db: Session):
    # Seed a photo to share.
    root = make_root(db, label="sh", abs_path="/tmp/sh")
    p = make_photo(db, root, rel_path="share-me.jpg")
    db.commit()

    # Create — POST /api/shares with a single photo_id.
    r = client.post("/api/shares", json={"photo_ids": [p.id]})
    assert r.status_code == 200, r.text
    created = r.json()
    share_id = created["id"]
    token = created["token"]
    assert created["revoked"] is False
    assert created["photo_count"] == 1

    # List — admin sees the new share.
    r = client.get("/api/shares")
    assert r.status_code == 200
    listed = r.json()
    assert any(s["id"] == share_id for s in listed)

    # Paginated list endpoint also surfaces it.
    r = client.get("/api/shares/page?page=1&page_size=10")
    assert r.status_code == 200
    page = r.json()
    assert page["total"] >= 1
    assert any(s["id"] == share_id for s in page["items"])

    # Public token endpoint resolves before revoke (no password set).
    r = client.get(f"/api/share/{token}")
    assert r.status_code == 200
    pub = r.json()
    assert pub["photo_count"] == 1
    assert pub["needs_password"] is False

    # Revoke — DELETE /api/shares/{id}.
    r = client.delete(f"/api/shares/{share_id}")
    assert r.status_code in (200, 204)

    # After revoke, the public token resolution returns 410/404.
    r = client.get(f"/api/share/{token}")
    assert r.status_code in (404, 410, 403)

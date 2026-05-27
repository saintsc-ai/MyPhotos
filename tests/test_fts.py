"""Tests for app.fts — the FTS5 trigram unified-search helpers.

The whole point of going FTS5 was Korean substring search, so the
must-have cases are:

- A non-FTS schema gracefully no-ops (so deploys without the migration
  don't break).
- rebuild_photo writes a row that MATCHes Korean and English substrings.
- delete_photo / re-rebuild round-trip works (contentless mode would
  have failed here — regression guard).
- build_match_query refuses short queries so the route can fall back.
"""

from __future__ import annotations

from sqlalchemy import text

from app import fts
from app.models import PhotoComment, PhotoTag, Tag
from tests.conftest import make_photo, make_root, make_user


def _match(db, q: str) -> list[int]:
    expr = fts.build_match_query(q)
    if expr is None:
        return []
    rows = db.execute(
        text(fts.matching_photo_ids_sql()).bindparams(match=expr)
    ).all()
    return [r[0] for r in rows]


def test_is_available_false_without_table(db):
    fts._reset_availability_cache()
    assert fts.is_available(db) is False
    # And every mutation helper must no-op rather than blow up so a
    # pre-migration deploy doesn't 500 on writes.
    fts.rebuild_photo(db, 1)
    fts.delete_photo(db, 1)
    fts.bulk_rebuild(db, [1, 2, 3])


def test_is_available_true_with_table(fts_db):
    assert fts.is_available(fts_db) is True


def test_rebuild_and_match_korean_substring(fts_db):
    """The reason for trigram: '양이바' inside '고양이바다' must hit."""
    user = make_user(fts_db, username="scsung")
    root = make_root(fts_db)
    p = make_photo(
        fts_db, root,
        rel_path="2024/베트남/IMG.HEIC",
        filename="IMG.HEIC",
        description="하노이 고양이바다 첫날",
        owner_user_id=user.id,
    )
    fts_db.commit()
    fts.rebuild_photo(fts_db, p.id)
    fts_db.commit()

    assert _match(fts_db, "고양이바다") == [p.id]
    # Trigram lets the substring inside it match too.
    assert _match(fts_db, "양이바") == [p.id]
    assert _match(fts_db, "하노이") == [p.id]
    # Path tokens and uploader name also live in the bag.
    assert _match(fts_db, "베트남") == [p.id]
    assert _match(fts_db, "scsung") == [p.id]


def test_rebuild_picks_up_tag_and_comment(fts_db):
    user = make_user(fts_db, username="u")
    root = make_root(fts_db)
    p = make_photo(fts_db, root, rel_path="x.jpg", description="meh")
    tag = Tag(name="고양이")
    fts_db.add(tag)
    fts_db.flush()
    fts_db.add(PhotoTag(photo_id=p.id, tag_id=tag.id))
    fts_db.add(PhotoComment(photo_id=p.id, user_id=user.id, body="저녁햇살"))
    fts_db.commit()
    fts.rebuild_photo(fts_db, p.id)
    fts_db.commit()
    assert _match(fts_db, "고양이") == [p.id]
    assert _match(fts_db, "저녁햇살") == [p.id]


def test_delete_and_re_rebuild_round_trip(fts_db):
    """Contentless FTS5 would reject the DELETE — this test would have
    failed during Phase C and prompted the switch to non-contentless."""
    user = make_user(fts_db, username="u")
    root = make_root(fts_db)
    p = make_photo(fts_db, root, rel_path="x.jpg", description="고양이")
    fts_db.commit()
    fts.rebuild_photo(fts_db, p.id)
    fts_db.commit()
    assert _match(fts_db, "고양이") == [p.id]

    fts.delete_photo(fts_db, p.id)
    fts_db.commit()
    assert _match(fts_db, "고양이") == []

    fts.rebuild_photo(fts_db, p.id)
    fts_db.commit()
    assert _match(fts_db, "고양이") == [p.id]


def test_bulk_rebuild_indexes_all(fts_db):
    root = make_root(fts_db)
    a = make_photo(fts_db, root, rel_path="a.jpg", description="alpha")
    b = make_photo(fts_db, root, rel_path="b.jpg", description="bravo")
    fts_db.commit()
    fts.bulk_rebuild(fts_db, [a.id, b.id])
    fts_db.commit()
    assert _match(fts_db, "alpha") == [a.id]
    assert _match(fts_db, "bravo") == [b.id]


def test_build_match_query_rejects_short(db):
    assert fts.build_match_query("") is None
    assert fts.build_match_query(" ") is None
    assert fts.build_match_query("ab") is None
    # 3 chars is the trigram minimum — accept.
    assert fts.build_match_query("abc") == '"abc"'


def test_build_match_query_escapes_quotes(db):
    # FTS5 doubles embedded quotes inside a phrase literal.
    assert fts.build_match_query('he said "hi"') == '"he said ""hi"""'


def test_build_match_query_strips_outer_space(db):
    assert fts.build_match_query("  cat  ") == '"cat"'

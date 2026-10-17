"""_replace_auto_tags with repeated names (YOLO: two people → "사람" twice)
on an autoflush-off session, which is how the workers' SessionLocal is
configured. The duplicate used to slip past the pending-row check and fail
the whole objects stage on the (photo, tag, source) unique constraint."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import PhotoAutoTag, Tag
from app.worker_ml.jobs import _replace_auto_tags
from tests.conftest import make_photo, make_root


def test_duplicate_names_with_existing_tag(engine):
    with Session(engine, autoflush=False) as db:
        root = make_root(db)
        photo = make_photo(db, root)
        db.add(Tag(name="사람", source="auto-yolo"))   # existing tag → no flush
        db.commit()

        _replace_auto_tags(db, photo.id, "auto-yolo", ["사람", "차", "사람"])
        db.commit()
        _replace_auto_tags(db, photo.id, "auto-yolo", ["사람", "사람"])   # re-run
        db.commit()

        rows = db.execute(
            select(Tag.name).join(PhotoAutoTag, PhotoAutoTag.tag_id == Tag.id)
            .where(PhotoAutoTag.photo_id == photo.id)
        ).scalars().all()
        assert rows == ["사람"]

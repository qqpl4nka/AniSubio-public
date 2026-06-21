from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from anisubio.db import Base
from anisubio.models import ReviewItem, StorageObject, SyncJob, UnresolvedSubtitle
from anisubio.services.review_queue import (
    add_unresolved_subtitle,
    backfill_review_queue,
    classify_failure,
)


def test_failure_categories() -> None:
    assert classify_failure("") == "unknown_error"
    assert classify_failure("В карточке нет архивов") == "no_archives"
    assert classify_failure("Failed the read enough data") == "broken_archive"
    assert classify_failure("unresolved=3") == "unmapped_filenames"


def test_unresolved_file_enters_review_queue() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as db:
        storage = StorageObject(
            backend="telegram",
            object_id="file-id",
            checksum="a" * 64,
            size_bytes=10,
            original_filename="opening.ass",
            media_type="text/x-ssa",
        )
        db.add(storage)
        db.flush()
        unresolved = UnresolvedSubtitle(
            kitsu_id=11,
            fansubs_archive_id=123,
            original_filename="opening.ass",
            checksum="a" * 64,
            storage_object_id=storage.id,
        )
        db.add(unresolved)
        db.flush()
        add_unresolved_subtitle(db, unresolved)
        db.commit()

        review = db.query(ReviewItem).one()
        assert review.item_type == "subtitle_file"
        assert review.category == "unmapped_filename"


def test_backfill_is_idempotent() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as db:
        db.add(
            SyncJob(
                kitsu_id=22,
                status="failed",
                attempts=3,
                error="404 metadata",
            )
        )
        db.commit()
        assert backfill_review_queue(db) == (1, 0)
        assert backfill_review_queue(db) == (0, 0)

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from anisubio.db import Base
from anisubio.models import (
    ReviewAnalysis,
    ReviewItem,
    StorageObject,
    UnresolvedSubtitle,
)
from anisubio.review_worker import analyze_batch


def test_review_worker_detects_episode_without_resolving_item() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as db:
        storage = StorageObject(
            backend="telegram",
            object_id="file",
            checksum="a" * 64,
            size_bytes=10,
            original_filename="Anime OVA2.ass",
            media_type="text/x-ssa",
        )
        db.add(storage)
        db.flush()
        unresolved = UnresolvedSubtitle(
            kitsu_id=11,
            fansubs_archive_id=100,
            original_filename="Anime OVA2.ass",
            checksum="a" * 64,
            storage_object_id=storage.id,
        )
        db.add(unresolved)
        db.flush()
        review = ReviewItem(
            dedupe_key=f"unresolved_subtitle:{unresolved.id}",
            item_type="subtitle_file",
            category="unmapped_filename",
            kitsu_id=11,
            unresolved_subtitle_id=unresolved.id,
            summary=unresolved.original_filename,
        )
        db.add(review)
        db.commit()

        assert analyze_batch(db, 10) == 1
        analysis = db.query(ReviewAnalysis).one()
        assert analysis.candidate_episode == 2
        assert analysis.confidence_percent == 98
        assert review.status == "pending_review"
        assert analyze_batch(db, 10) == 0

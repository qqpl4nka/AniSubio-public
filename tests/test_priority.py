import json

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from anisubio.db import Base
from anisubio.models import FansubsCatalogItem
from anisubio.priority import (
    catalog_priority_rank,
    priority_rank_for_mal_id,
    priority_rank_for_titles,
    promote_imdb_priority_jobs,
)
from anisubio.services.jobs import enqueue_sync_job


def test_imdb_top_500_matches_english_and_original_titles() -> None:
    assert priority_rank_for_titles("Attack on Titan") == 1
    assert priority_rank_for_titles("Shingeki no Kyojin") == 1
    assert priority_rank_for_titles("An unrelated anime") is None


def test_catalog_alias_can_mark_item_as_priority() -> None:
    item = FansubsCatalogItem(
        fansubs_id=1,
        page_url="http://fansubs.ru/base.php?id=1",
        canonical_title="Атака титанов",
        aliases_json=json.dumps(["Shingeki no Kyojin"]),
    )

    assert catalog_priority_rank(item) == 1


def test_imdb_based_top_100_matches_mal_id() -> None:
    assert priority_rank_for_mal_id(35847) == 98
    assert priority_rank_for_mal_id(999_999_999) is None


def test_existing_pending_job_is_promoted_to_priority_one() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as db:
        item = FansubsCatalogItem(
            fansubs_id=1,
            page_url="http://fansubs.ru/base.php?id=1",
            canonical_title="Attack on Titan",
            aliases_json="[]",
        )
        db.add(item)
        db.commit()
        job = enqueue_sync_job(
            db,
            11,
            reason="vacuum",
            priority=100,
            fansubs_id=1,
            resolved_title="Attack on Titan",
        )

        assert promote_imdb_priority_jobs(db) == 1
        assert job.priority == 1
        assert job.reason == "imdb_priority"

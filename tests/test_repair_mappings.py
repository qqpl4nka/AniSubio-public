import json

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from anisubio.db import Base
from anisubio.models import FansubsCatalogItem, ReviewItem, SubtitleAsset
from anisubio.repair_mappings import repair_catalog_outliers_in_session


def add_catalog(db: Session, fansubs_id: int, episodes: int = 25) -> None:
    db.add(
        FansubsCatalogItem(
            fansubs_id=fansubs_id,
            page_url=f"http://fansubs.test/{fansubs_id}",
            canonical_title="Example",
            aliases_json="[]",
            media_kind="ТВ",
            episode_count=episodes,
            mal_id=1,
            kitsu_id=100,
            resolution_status="resolved",
        )
    )


def add_asset(db: Session, asset_id: int, episode: int, filename: str) -> None:
    db.add(
        SubtitleAsset(
            id=asset_id,
            kitsu_id=100,
            fansubs_id=1,
            episode=episode,
            language="rus",
            display_name=filename,
            original_filename=filename,
            media_type="text/x-ssa",
            checksum=str(asset_id).zfill(64),
        )
    )


def test_repairs_crc_false_positive_and_queues_ambiguous_outlier() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as db:
        add_catalog(db, 1)
        add_asset(db, 1, 7923, "Anime - 19 [E7923CB9].ass")
        add_asset(db, 2, 49, "Anime - 49.ass")
        db.commit()

        result = repair_catalog_outliers_in_session(db, apply=True)

        assert result == {
            "examined": 2,
            "corrected": 1,
            "duplicates": 0,
            "reviewed": 1,
        }
        assert db.get(SubtitleAsset, 1).episode == 19
        assert db.get(SubtitleAsset, 2).episode == 49
        review = db.query(ReviewItem).one()
        assert review.category == "episode_range_outlier"
        assert json.loads(review.payload_json)["detected_episode"] == 49


def test_does_not_move_sequel_or_special_into_parent_series() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as db:
        add_catalog(db, 1)
        add_asset(db, 1, 82, "Example R2 07 (DivX6.82).ass")
        add_asset(db, 2, 1983, "Example Safety Education Anime #1 (1983).ass")
        db.commit()

        result = repair_catalog_outliers_in_session(db, apply=True)

        assert result["corrected"] == 0
        assert result["reviewed"] == 2
        assert db.get(SubtitleAsset, 1).episode == 82
        assert db.get(SubtitleAsset, 2).episode == 1983

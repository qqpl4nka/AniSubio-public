import json

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from anisubio.audit_mappings import audit_database, audit_external_mappings
from anisubio.db import Base
from anisubio.models import ExternalIdMapping, FansubsCatalogItem, SubtitleAsset


def catalog_item(
    fansubs_id: int,
    kitsu_id: int,
    *,
    mal_id: int = 1,
    title: str = "Example",
    aliases: tuple[str, ...] = (),
    episode_count: int = 12,
) -> FansubsCatalogItem:
    return FansubsCatalogItem(
        fansubs_id=fansubs_id,
        page_url=f"http://fansubs.test/{fansubs_id}",
        canonical_title=title,
        aliases_json=json.dumps(aliases),
        media_kind="ТВ",
        episode_count=episode_count,
        mal_id=mal_id,
        kitsu_id=kitsu_id,
        resolution_status="resolved",
    )


def test_detects_asset_catalog_mismatch_and_episode_outlier() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        db.add(catalog_item(10, 100, episode_count=12))
        db.add(
            SubtitleAsset(
                kitsu_id=200,
                fansubs_id=10,
                episode=40,
                language="rus",
                display_name="wrong",
                original_filename="wrong.ass",
                media_type="text/x-ssa",
                checksum="a" * 64,
            )
        )
        db.commit()

        findings = audit_database(db)
        codes = {finding.code for finding in findings}
        assert "asset_catalog_kitsu_mismatch" in codes
        assert "episode_range_outlier" in codes


def test_consistent_asset_has_no_finding() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        db.add(catalog_item(10, 100))
        db.add(
            SubtitleAsset(
                kitsu_id=100,
                fansubs_id=10,
                episode=1,
                language="rus",
                display_name="good",
                original_filename="good.ass",
                media_type="text/x-ssa",
                checksum="b" * 64,
            )
        )
        db.commit()
        assert audit_database(db) == []


def test_manual_verified_asset_can_bypass_unresolved_source() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        item = catalog_item(10, 100)
        item.kitsu_id = None
        item.mal_id = None
        item.resolution_status = "unresolved"
        db.add(item)
        db.add(
            SubtitleAsset(
                kitsu_id=200,
                fansubs_id=10,
                manual_verified=1,
                episode=1,
                language="rus",
                display_name="reviewed",
                original_filename="reviewed.ass",
                media_type="text/x-ssa",
                checksum="c" * 64,
            )
        )
        db.commit()
        assert audit_database(db) == []


def test_quarantined_asset_is_not_reported_as_active_mismatch() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        db.add(catalog_item(10, 100))
        db.add(
            SubtitleAsset(
                kitsu_id=999,
                fansubs_id=10,
                mapping_quarantined=1,
                episode=999,
                language="rus",
                display_name="quarantined",
                original_filename="quarantined.ass",
                media_type="text/x-ssa",
                checksum="d" * 64,
            )
        )
        db.commit()
        assert audit_database(db) == []


@pytest.mark.anyio
async def test_external_title_detects_other_exact_kitsu() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)

    async def fake_fetch(_url: str) -> dict:
        return {"meta": {"name": "Bleach: Thousand-Year Blood War"}}

    with Session(engine) as db:
        db.add(catalog_item(1, 244, title="Bleach", mal_id=269))
        db.add(
            catalog_item(
                2,
                43078,
                title="Блич [ТВ-2]",
                aliases=("Bleach: Thousand-Year Blood War",),
                mal_id=41467,
            )
        )
        db.add(
            ExternalIdMapping(
                external_id="tt14986406",
                season=1,
                tvdb_id=74796,
                kitsu_id=244,
            )
        )
        db.commit()

        findings = await audit_external_mappings(db, fake_fetch)
        assert len(findings) == 1
        assert findings[0].severity == "critical"
        assert findings[0].code == "external_title_maps_other_kitsu"
        assert findings[0].evidence["exact_catalog_kitsu_ids"] == [43078]

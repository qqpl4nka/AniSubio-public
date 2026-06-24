import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from anisubio.db import Base
from anisubio.models import ExternalIdMapping, FansubsCatalogItem
from anisubio.services import external_ids


@pytest.mark.anyio
async def test_resolves_and_caches_imdb_series(monkeypatch) -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    responses = iter(
        [
            {"meta": {"tvdb_id": 272074}},
            {
                "data": [
                    {
                        "relationships": {
                            "item": {"data": {"type": "anime", "id": "7712"}}
                        }
                    }
                ]
            },
        ]
    )

    async def fake_get_json(url):
        return next(responses)

    monkeypatch.setattr(external_ids, "_get_json", fake_get_json)
    with Session(engine, expire_on_commit=False) as db:
        assert await external_ids.resolve_imdb_series(
            db, "tt3114390", 1
        ) == 7712
        assert db.query(ExternalIdMapping).one().tvdb_id == 272074


@pytest.mark.anyio
async def test_catalog_title_overrides_wrong_franchise_tvdb_mapping(
    monkeypatch,
) -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)

    async def fake_get_json(url):
        assert "cinemeta" in url
        return {
            "meta": {
                "tvdb_id": 74796,
                "name": "Bleach: Thousand-Year Blood War",
            }
        }

    monkeypatch.setattr(external_ids, "_get_json", fake_get_json)
    with Session(engine, expire_on_commit=False) as db:
        db.add(
            FansubsCatalogItem(
                fansubs_id=6991,
                page_url="http://fansubs.ru/base.php?id=6991",
                canonical_title="Блич [ТВ-2]",
                aliases_json=(
                    '["Bleach: Thousand-Year Blood War", '
                    '"Bleach: Sennen Kessen-hen"]'
                ),
                media_kind="ТВ",
                mal_id=41467,
                kitsu_id=43078,
                resolution_status="resolved",
            )
        )
        db.commit()

        assert await external_ids.resolve_imdb_series(
            db,
            "tt14986406",
            1,
        ) == 43078
        assert db.query(ExternalIdMapping).one().kitsu_id == 43078

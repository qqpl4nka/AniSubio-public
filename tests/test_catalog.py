import json

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from anisubio.db import Base
from anisubio.models import FansubsCatalogItem
from anisubio.services.catalog import (
    CatalogMetadataResolver,
    FansubsCatalogCrawler,
)


class FakeCatalogClient:
    def __init__(self, *, payload=b"", json_responses=None):
        self.payload = payload
        self.json_responses = list(json_responses or [])
        self.urls = []

    async def request(self, method, url, **kwargs):
        self.urls.append(url)
        return self.payload, {}

    async def get_json(self, url, headers=None):
        self.urls.append(url)
        return self.json_responses.pop(0)


@pytest.mark.anyio
async def test_catalog_page_deduplicates_aliases() -> None:
    html = """
      <a href="base.php?id=274">Naruto <small>(ТВ)</small></a>
      <a href="base.php?id=274">Наруто <small>(ТВ)</small></a>
      <a href="base.php?id=1555">Naruto Shippuuden <small>(ТВ)</small></a>
    """
    client = FakeCatalogClient(payload=html.encode("cp1251"))
    crawler = FansubsCatalogCrawler(client)
    cards = await crawler.read_index_page("N", "http://fansubs.ru/base.php?l=n")

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        assert crawler.upsert_cards(db, "N", cards) == 2
        naruto = db.get(FansubsCatalogItem, 274)
        assert set(json.loads(naruto.aliases_json)) == {"Naruto", "Наруто"}


@pytest.mark.anyio
async def test_reverse_mapping_shikimori_mal_to_kitsu() -> None:
    client = FakeCatalogClient(
        json_responses=[
            [{"id": 20, "name": "Naruto", "russian": "Наруто"}],
            {
                "data": [
                    {
                        "relationships": {
                            "item": {"data": {"type": "anime", "id": "11"}}
                        }
                    }
                ]
            },
        ]
    )
    item = FansubsCatalogItem(
        fansubs_id=274,
        page_url="http://fansubs.ru/base.php?id=274",
        canonical_title="Naruto",
        aliases_json=json.dumps(["Naruto", "Наруто"], ensure_ascii=False),
    )

    assert await CatalogMetadataResolver(client).resolve_one(item) == (20, 11)


@pytest.mark.anyio
async def test_ambiguous_title_uses_episode_count_and_year() -> None:
    detail = (
        "<html><body>Общая информация: ТВ "
        "(03.10.2002 - 08.02.2007), 220 эп.</body></html>"
    ).encode("cp1251")
    client = FakeCatalogClient(
        payload=detail,
        json_responses=[
            [
                {
                    "id": 20,
                    "name": "Naruto",
                    "russian": "Наруто",
                    "kind": "tv",
                    "episodes": 220,
                    "aired_on": "2002-10-03",
                },
                {
                    "id": 54688,
                    "name": "Naruto (Shinsaku Anime)",
                    "russian": "Наруто",
                    "kind": "tv",
                    "episodes": 4,
                    "aired_on": None,
                },
            ],
            {
                "data": [
                    {
                        "relationships": {
                            "item": {"data": {"type": "anime", "id": "11"}}
                        }
                    }
                ]
            },
        ],
    )
    item = FansubsCatalogItem(
        fansubs_id=274,
        page_url="http://fansubs.ru/base.php?id=274",
        canonical_title="Наруто",
        aliases_json=json.dumps(["Naruto", "Наруто"], ensure_ascii=False),
        media_kind="ТВ",
    )

    assert await CatalogMetadataResolver(client).resolve_one(item) == (20, 11)
    assert item.episode_count == 220
    assert item.start_year == 2002

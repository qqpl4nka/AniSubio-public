import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from anisubio.db import Base
from anisubio.models import ExternalIdMapping
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

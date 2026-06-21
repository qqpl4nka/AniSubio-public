from __future__ import annotations

from urllib.parse import quote

import aiohttp
from sqlalchemy import select
from sqlalchemy.orm import Session

from anisubio.models import ExternalIdMapping


CINEMETA_META_URL = "https://v3-cinemeta.strem.io/meta/series/{imdb_id}.json"
KITSU_MAPPING_URL = (
    "https://kitsu.io/api/edge/mappings"
    "?filter%5BexternalSite%5D={external_site}"
    "&filter%5BexternalId%5D={external_id}"
    "&include=item"
)


async def _get_json(url: str) -> dict:
    timeout = aiohttp.ClientTimeout(total=5)
    accept = (
        "application/vnd.api+json"
        if "kitsu.io/" in url
        else "application/json"
    )
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(
            url,
            headers={"Accept": accept},
        ) as response:
            response.raise_for_status()
            return await response.json()


async def _kitsu_for_tvdb(tvdb_id: int, season: int) -> int | None:
    lookups = [("thetvdb", f"{tvdb_id}/{season}")]
    if season == 1:
        lookups.append(("thetvdb/series", str(tvdb_id)))
    for external_site, external_id in lookups:
        payload = await _get_json(
            KITSU_MAPPING_URL.format(
                external_site=quote(external_site, safe=""),
                external_id=quote(external_id, safe=""),
            )
        )
        kitsu_ids = {
            int(row["relationships"]["item"]["data"]["id"])
            for row in payload.get("data", [])
            if row.get("relationships", {})
            .get("item", {})
            .get("data", {})
            .get("type")
            == "anime"
        }
        if len(kitsu_ids) == 1:
            return kitsu_ids.pop()
    return None


async def resolve_imdb_series(
    db: Session,
    imdb_id: str,
    season: int,
) -> int | None:
    cached = db.scalar(
        select(ExternalIdMapping).where(
            ExternalIdMapping.external_id == imdb_id,
            ExternalIdMapping.season == season,
        )
    )
    if cached is not None:
        return cached.kitsu_id

    payload = await _get_json(CINEMETA_META_URL.format(imdb_id=imdb_id))
    tvdb_id = payload.get("meta", {}).get("tvdb_id")
    if not isinstance(tvdb_id, int):
        return None
    kitsu_id = await _kitsu_for_tvdb(tvdb_id, season)
    if kitsu_id is None:
        return None
    db.add(
        ExternalIdMapping(
            external_id=imdb_id,
            season=season,
            tvdb_id=tvdb_id,
            kitsu_id=kitsu_id,
        )
    )
    db.commit()
    return kitsu_id

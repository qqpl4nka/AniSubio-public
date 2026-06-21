from __future__ import annotations

from functools import lru_cache
from importlib.resources import files
import json
import re
import unicodedata

from sqlalchemy import select
from sqlalchemy.orm import Session

from anisubio.models import FansubsCatalogItem, JobStatus, SyncJob


IMDB_PRIORITY = 1


def normalize_priority_title(value: str) -> str:
    value = unicodedata.normalize("NFKC", value).casefold().replace("ё", "е")
    return re.sub(r"[^a-zа-я0-9]+", "", value)


@lru_cache
def imdb_priority_titles() -> dict[str, int]:
    resource = files("anisubio").joinpath("data/imdb_top_500_anime.json")
    payload = json.loads(resource.read_text(encoding="utf-8"))
    result: dict[str, int] = {}
    for item in payload["items"]:
        for value in (item.get("title"), item.get("original_title")):
            normalized = normalize_priority_title(value or "")
            if normalized:
                result.setdefault(normalized, int(item["rank"]))
    return result


@lru_cache
def imdb_priority_mal_ids() -> dict[int, int]:
    resource = files("anisubio").joinpath("data/imdb_priority_mal_ids.json")
    payload = json.loads(resource.read_text(encoding="utf-8"))
    result: dict[int, int] = {}
    for item in payload["items"]:
        result.setdefault(int(item["mal_id"]), int(item["rank"]))
    return result


def priority_rank_for_mal_id(mal_id: int | None) -> int | None:
    return imdb_priority_mal_ids().get(mal_id) if mal_id is not None else None


def priority_rank_for_titles(*titles: str | None) -> int | None:
    ranks = [
        imdb_priority_titles()[normalized]
        for title in titles
        if title and (normalized := normalize_priority_title(title))
        in imdb_priority_titles()
    ]
    return min(ranks) if ranks else None


def catalog_priority_rank(item: FansubsCatalogItem) -> int | None:
    try:
        aliases = json.loads(item.aliases_json or "[]")
    except json.JSONDecodeError:
        aliases = []
    return priority_rank_for_titles(item.canonical_title, *aliases)


def promote_imdb_priority_jobs(db: Session) -> int:
    jobs = db.scalars(
        select(SyncJob).where(
            SyncJob.status == JobStatus.PENDING.value,
            SyncJob.priority > IMDB_PRIORITY,
        )
    ).all()
    promoted = 0
    for job in jobs:
        titles: list[str | None] = [job.resolved_title]
        if job.fansubs_id is not None:
            item = db.get(FansubsCatalogItem, job.fansubs_id)
            if item is not None:
                titles.append(item.canonical_title)
                try:
                    titles.extend(json.loads(item.aliases_json or "[]"))
                except json.JSONDecodeError:
                    pass
        if (
            priority_rank_for_mal_id(job.resolved_mal_id) is None
            and priority_rank_for_titles(*titles) is None
        ):
            continue
        job.priority = IMDB_PRIORITY
        job.reason = "imdb_priority"
        promoted += 1
    if promoted:
        db.commit()
    return promoted

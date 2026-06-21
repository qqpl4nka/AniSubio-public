from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import parse_qs, quote, urljoin, urlparse

from bs4 import BeautifulSoup
from sqlalchemy import select
from sqlalchemy.orm import Session

from anisubio.models import FansubsCatalogItem, VacuumState, utcnow
from anisubio.priority import (
    IMDB_PRIORITY,
    catalog_priority_rank,
    promote_imdb_priority_jobs,
    priority_rank_for_mal_id,
)
from anisubio.services.jobs import enqueue_sync_job


FANSUBS_BASE_URL = "http://fansubs.ru/"
FANSUBS_INDEX_URL = "http://fansubs.ru/base.php"
SHIKIMORI_SEARCH_URL = "https://shikimori.one/api/animes"
KITSU_MAPPINGS_URL = "https://kitsu.io/api/edge/mappings"


class CatalogHttpClient(Protocol):
    async def request(self, method: str, url: str, **kwargs) -> tuple[bytes, dict]: ...

    async def get_json(self, url: str, headers: dict[str, str] | None = None): ...


def normalize_title(value: str) -> str:
    value = unicodedata.normalize("NFKC", value).casefold().replace("ё", "е")
    return re.sub(r"[^a-zа-я0-9]+", "", value)


@dataclass(frozen=True)
class CatalogCard:
    fansubs_id: int
    title: str
    media_kind: str | None
    page_url: str


def _split_title_and_kind(text: str) -> tuple[str, str | None]:
    match = re.match(r"^(.*?)\s+\(([^()]*)\)\s*$", text)
    if not match:
        return text.strip(), None
    return match.group(1).strip(), match.group(2).strip()


class FansubsCatalogCrawler:
    def __init__(self, client: CatalogHttpClient):
        self.client = client

    async def discover_index_urls(self) -> tuple[tuple[str, str], ...]:
        payload, _ = await self.client.request("GET", FANSUBS_INDEX_URL)
        soup = BeautifulSoup(payload.decode("cp1251", "replace"), "html.parser")
        pages: list[tuple[str, str]] = []
        for link in soup.select('a[href^="base.php?l="]'):
            href = link.get("href", "")
            label = link.get_text(" ", strip=True)
            url = urljoin(FANSUBS_BASE_URL, href)
            item = (label, url)
            if item not in pages:
                pages.append(item)
        return tuple(pages)

    async def read_index_page(self, letter: str, url: str) -> tuple[CatalogCard, ...]:
        payload, _ = await self.client.request("GET", url)
        soup = BeautifulSoup(payload.decode("cp1251", "replace"), "html.parser")
        cards: list[CatalogCard] = []
        for link in soup.select('a[href^="base.php?id="]'):
            href = link.get("href", "")
            query = parse_qs(urlparse(href).query)
            raw_id = (query.get("id") or [""])[0]
            if not raw_id.isdigit():
                continue
            text = " ".join(link.stripped_strings)
            title, media_kind = _split_title_and_kind(text)
            cards.append(
                CatalogCard(
                    fansubs_id=int(raw_id),
                    title=title,
                    media_kind=media_kind,
                    page_url=urljoin(FANSUBS_BASE_URL, href),
                )
            )
        return tuple(cards)

    def upsert_cards(
        self,
        db: Session,
        letter: str,
        cards: tuple[CatalogCard, ...],
    ) -> int:
        observed: dict[int, dict] = {}
        for card in cards:
            entry = observed.setdefault(
                card.fansubs_id,
                {
                    "page_url": card.page_url,
                    "canonical_title": card.title,
                    "media_kind": card.media_kind,
                    "index_letter": letter,
                    "aliases": [],
                },
            )
            if card.title not in entry["aliases"]:
                entry["aliases"].append(card.title)

        for fansubs_id, values in observed.items():
            item = db.get(FansubsCatalogItem, fansubs_id)
            if item is None:
                item = FansubsCatalogItem(
                    fansubs_id=fansubs_id,
                    **{key: value for key, value in values.items() if key != "aliases"},
                )
            existing_aliases = set(json.loads(item.aliases_json or "[]"))
            existing_aliases.update(values["aliases"])
            item.aliases_json = json.dumps(
                sorted(existing_aliases), ensure_ascii=False
            )
            item.page_url = values["page_url"]
            item.canonical_title = values["canonical_title"]
            item.media_kind = values["media_kind"]
            item.index_letter = values["index_letter"]
            item.updated_at = utcnow()
            db.add(item)
        db.commit()
        return len(observed)

    async def refresh_catalog(self, db: Session) -> int:
        pages = await self.discover_index_urls()
        total_ids: set[int] = set()
        for letter, url in pages:
            cards = await self.read_index_page(letter, url)
            total_ids.update(card.fansubs_id for card in cards)
            self.upsert_cards(db, letter, cards)

        state = db.get(VacuumState, "catalog_last_scan")
        if state is None:
            state = VacuumState(key="catalog_last_scan", value=utcnow().isoformat())
        else:
            state.value = utcnow().isoformat()
        db.add(state)
        db.commit()
        return len(total_ids)


class CatalogMetadataResolver:
    def __init__(self, client: CatalogHttpClient):
        self.client = client

    async def enrich_details(self, item: FansubsCatalogItem) -> None:
        payload, _ = await self.client.request("GET", item.page_url)
        soup = BeautifulSoup(payload.decode("cp1251", "replace"), "html.parser")
        text = " ".join(soup.stripped_strings)
        episode_match = re.search(r"(\d+)\s*эп\.", text, re.IGNORECASE)
        if episode_match:
            item.episode_count = int(episode_match.group(1))
        year_match = re.search(
            r"\((?:\d{2}\.\d{2}\.)?(\d{4})(?:\s*[-–]|[),])",
            text,
        )
        if year_match:
            item.start_year = int(year_match.group(1))

    @staticmethod
    def _kind_supported(fansubs_kind: str | None) -> bool:
        return not fansubs_kind or fansubs_kind.casefold() not in {
            "игровой тв",
            "сборник",
            "манга",
        }

    @staticmethod
    def _kind_matches(fansubs_kind: str | None, shikimori_kind: str | None) -> bool:
        if not fansubs_kind or not shikimori_kind:
            return True
        mapping = {
            "тв": "tv",
            "ova": "ova",
            "ona": "ona",
            "фильм": "movie",
            "спецвыпуск": "special",
            "cпецвыпуск": "special",
        }
        normalized = fansubs_kind.casefold()
        if not CatalogMetadataResolver._kind_supported(fansubs_kind):
            return False
        expected = mapping.get(normalized)
        return expected is None or expected == shikimori_kind.casefold()

    async def resolve_one(
        self,
        item: FansubsCatalogItem,
    ) -> tuple[int, int] | None:
        if not self._kind_supported(item.media_kind):
            return None
        aliases = json.loads(item.aliases_json or "[]")
        search_terms = list(dict.fromkeys([item.canonical_title, *aliases]))
        matches: dict[int, dict] = {}
        normalized_aliases = {normalize_title(value) for value in search_terms}

        for term in search_terms[:3]:
            payload = await self.client.get_json(
                SHIKIMORI_SEARCH_URL
                + "?search="
                + quote(term)
                + "&limit=10",
                headers={"Accept": "application/json"},
            )
            for anime in payload:
                names = {
                    normalize_title(str(anime.get("name") or "")),
                    normalize_title(str(anime.get("russian") or "")),
                }
                if (
                    names & normalized_aliases
                    and self._kind_matches(
                        item.media_kind,
                        anime.get("kind"),
                    )
                ):
                    matches[int(anime["id"])] = anime
            if matches:
                break

        # Titles alone are not enough: anime franchises often reuse the same
        # localized name for a TV series, movie, OVA and specials. Apply the
        # release kind even when an exact title produced only one candidate.
        matches = {
            mal_id: anime
            for mal_id, anime in matches.items()
            if self._kind_matches(item.media_kind, anime.get("kind"))
        }

        if len(matches) > 1:
            await self.enrich_details(item)
            narrowed = {
                mal_id: anime
                for mal_id, anime in matches.items()
                if (
                    item.episode_count is None
                    or int(anime.get("episodes") or 0) == item.episode_count
                )
                and (
                    item.start_year is None
                    or str(anime.get("aired_on") or "").startswith(
                        str(item.start_year)
                    )
                )
            }
            if narrowed:
                matches = narrowed
        if len(matches) != 1:
            return None
        mal_id = next(iter(matches))
        query = (
            KITSU_MAPPINGS_URL
            + "?filter%5BexternalSite%5D=myanimelist%2Fanime"
            + f"&filter%5BexternalId%5D={mal_id}"
            + "&include=item"
        )
        mappings = await self.client.get_json(
            query,
            headers={"Accept": "application/vnd.api+json"},
        )
        kitsu_ids = {
            int(row["relationships"]["item"]["data"]["id"])
            for row in mappings.get("data", [])
            if row.get("relationships", {}).get("item", {}).get("data", {}).get("type")
            == "anime"
        }
        if len(kitsu_ids) != 1:
            return None
        return mal_id, kitsu_ids.pop()


async def resolve_catalog_batch(
    db: Session,
    client: CatalogHttpClient,
    batch_size: int,
) -> int:
    candidates = db.scalars(
        select(FansubsCatalogItem)
        .where(FansubsCatalogItem.resolution_status.in_(["pending", "retry"]))
        .order_by(FansubsCatalogItem.updated_at)
    ).all()
    candidates.sort(
        key=lambda item: (
            catalog_priority_rank(item) is None,
            catalog_priority_rank(item) or 10_000,
            item.updated_at,
        )
    )
    items = candidates[:batch_size]
    resolver = CatalogMetadataResolver(client)
    resolved = 0
    for item in items:
        try:
            result = await resolver.resolve_one(item)
            if result is None:
                item.resolution_status = "unresolved"
                item.resolution_detail = "No unique exact Shikimori/Kitsu match"
            else:
                item.mal_id, item.kitsu_id = result
                item.resolution_status = "resolved"
                item.resolution_detail = None
                enqueue_sync_job(
                    db,
                    item.kitsu_id,
                    reason="vacuum",
                    priority=(
                        IMDB_PRIORITY
                        if (
                            priority_rank_for_mal_id(item.mal_id) is not None
                            or catalog_priority_rank(item) is not None
                        )
                        else 100
                    ),
                    fansubs_id=item.fansubs_id,
                    source_page_url=item.page_url,
                    resolved_mal_id=item.mal_id,
                    resolved_title=item.canonical_title,
                )
                resolved += 1
        except Exception as exc:
            item.resolution_status = "retry"
            item.resolution_detail = str(exc)
        item.updated_at = utcnow()
        db.add(item)
        db.commit()
    promote_imdb_priority_jobs(db)
    return resolved

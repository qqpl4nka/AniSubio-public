from __future__ import annotations

import argparse
import asyncio
import logging
import os
import random
import re
import socket
import time
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import urlencode, urljoin
from urllib.parse import urlparse
from uuid import uuid4

import aiohttp
from aiohttp_socks import ProxyConnector
from bs4 import BeautifulSoup
from filelock import FileLock
from sqlalchemy import select
from sqlalchemy.orm import Session

from anisubio.config import Settings, get_settings
from anisubio.db import SessionLocal, create_schema
from anisubio.models import (
    FansubsCatalogItem,
    StorageObject,
    SubtitleAsset,
    SyncJob,
    SyncRecord,
    UnresolvedSubtitle,
    utcnow,
)
from anisubio.services.archive import ArchiveError, extract_subtitles
from anisubio.services.jobs import (
    claim_next_job,
    complete_job,
    enqueue_sync_job,
    fail_job,
    recover_interrupted_jobs,
)
from anisubio.services.mapper import episode_from_filename
from anisubio.services.review_queue import add_unresolved_subtitle
from anisubio.storage import StorageMetadata, TelegramStorage
from anisubio.storage.factory import create_telegram_storage
from anisubio.storage.telegram import TelegramStorageError


LOG = logging.getLogger("anisubio.sync")
KITSU_MAPPING_URL = (
    "https://kitsu.io/api/edge/anime/{kitsu_id}/mappings?page%5Blimit%5D=20"
)
SHIKIMORI_ANIME_URL = "https://shikimori.one/api/animes/{mal_id}"
FANSUBS_SEARCH_URL = "http://fansubs.ru/search.php"
FANSUBS_BASE_URL = "http://fansubs.ru/"

CLIENT_HEADERS = {
    # Browser-compatible metadata, while still honestly identifying this client.
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/126.0.0.0 Safari/537.36 "
        "AniSubio/0.1 (+https://github.com/qqpl4nka/AniSubio)"
    ),
    "Accept": "text/html,application/json,application/xhtml+xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.7",
    "Connection": "keep-alive",
}


class SyncError(RuntimeError):
    pass


class AmbiguousMatchError(SyncError):
    pass


@dataclass(frozen=True)
class AnimeMetadata:
    kitsu_id: int
    mal_id: int
    russian_title: str
    aliases: tuple[str, ...]


@dataclass(frozen=True)
class FansubsCandidate:
    title: str
    page_url: str


@dataclass(frozen=True)
class FansubsArchive:
    subtitle_id: int
    download_url: str


@dataclass(frozen=True)
class SyncResult:
    kitsu_id: int
    status: str
    imported: int = 0
    duplicates: int = 0
    unresolved: tuple[str, ...] = ()
    detail: str = ""


class PoliteHttpClient:
    """One serialized HTTP client with a delay before every request after the first."""

    def __init__(
        self,
        min_delay: float,
        max_delay: float,
        timeout: float,
        session: aiohttp.ClientSession | None = None,
        proxy_url: str = "",
        proxy_hosts: tuple[str, ...] = ("kitsu.io", "shikimori.one"),
        retries: int = 3,
        rate_limit_file: Path | None = None,
    ):
        if min_delay < 0 or max_delay < min_delay:
            raise ValueError("Некорректный диапазон задержки")
        self.min_delay = min_delay
        self.max_delay = max_delay
        self.timeout = aiohttp.ClientTimeout(total=timeout)
        self._session = session
        self._proxy_session: aiohttp.ClientSession | None = None
        self._owns_session = session is None
        self.proxy_url = proxy_url
        self.proxy_hosts = proxy_hosts
        self.retries = max(retries, 1)
        self.rate_limit_file = rate_limit_file
        self._lock = asyncio.Lock()
        self._last_request_at: float | None = None

    def _wait_for_global_rate_slot(self) -> None:
        if self.rate_limit_file is None:
            return
        lock = FileLock(str(self.rate_limit_file) + ".lock")
        with lock:
            previous = 0.0
            if self.rate_limit_file.is_file():
                try:
                    previous = float(
                        self.rate_limit_file.read_text(encoding="ascii")
                    )
                except (OSError, ValueError):
                    previous = 0.0
            delay = random.uniform(self.min_delay, self.max_delay)
            remaining = delay - (time.time() - previous)
            if remaining > 0:
                time.sleep(remaining)
            self.rate_limit_file.write_text(
                str(time.time()),
                encoding="ascii",
            )

    async def __aenter__(self) -> "PoliteHttpClient":
        if self._session is None:
            connector = aiohttp.TCPConnector(limit=2, limit_per_host=1)
            self._session = aiohttp.ClientSession(
                headers=CLIENT_HEADERS,
                timeout=self.timeout,
                connector=connector,
                raise_for_status=True,
            )
        if self.proxy_url:
            self._proxy_session = aiohttp.ClientSession(
                headers=CLIENT_HEADERS,
                timeout=self.timeout,
                connector=ProxyConnector.from_url(self.proxy_url),
                raise_for_status=True,
            )
        return self

    async def __aexit__(self, *_: object) -> None:
        if self._owns_session and self._session is not None:
            await self._session.close()
        if self._proxy_session is not None:
            await self._proxy_session.close()

    async def request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        data: bytes | dict[str, str] | None = None,
        max_bytes: int | None = None,
    ) -> tuple[bytes, aiohttp.typedefs.LooseHeaders]:
        if self._session is None:
            raise RuntimeError("PoliteHttpClient не открыт")
        async with self._lock:
            host = (urlparse(url).hostname or "").lower()
            selected_session = (
                self._proxy_session
                if self._proxy_session is not None and host in self.proxy_hosts
                else self._session
            )
            last_error: Exception | None = None
            for attempt in range(self.retries):
                if self.rate_limit_file is not None:
                    await asyncio.to_thread(self._wait_for_global_rate_slot)
                elif self._last_request_at is not None:
                    delay = random.uniform(self.min_delay, self.max_delay)
                    elapsed = time.monotonic() - self._last_request_at
                    if elapsed < delay:
                        await asyncio.sleep(delay - elapsed)
                self._last_request_at = time.monotonic()
                try:
                    async with selected_session.request(
                        method, url, headers=headers, data=data
                    ) as response:
                        chunks: list[bytes] = []
                        total = 0
                        async for chunk in response.content.iter_chunked(64 * 1024):
                            total += len(chunk)
                            if max_bytes is not None and total > max_bytes:
                                raise SyncError(
                                    "HTTP-ответ превышает разрешённый размер"
                                )
                            chunks.append(chunk)
                        return b"".join(chunks), dict(response.headers)
                except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                    last_error = exc
                    if attempt + 1 >= self.retries:
                        raise
            raise SyncError(f"HTTP request failed: {last_error}")

    async def get_json(
        self, url: str, headers: dict[str, str] | None = None
    ) -> dict:
        payload, _ = await self.request("GET", url, headers=headers)
        try:
            import json

            return json.loads(payload)
        except (UnicodeDecodeError, ValueError) as exc:
            raise SyncError(f"Некорректный JSON от {url}") from exc


class MetadataResolver:
    def __init__(self, client: PoliteHttpClient):
        self.client = client

    async def resolve(self, kitsu_id: int) -> AnimeMetadata:
        mappings = await self.client.get_json(
            KITSU_MAPPING_URL.format(kitsu_id=kitsu_id),
            headers={"Accept": "application/vnd.api+json"},
        )
        mal_ids = {
            int(item["attributes"]["externalId"])
            for item in mappings.get("data", [])
            if item.get("attributes", {}).get("externalSite")
            == "myanimelist/anime"
            and str(item.get("attributes", {}).get("externalId", "")).isdigit()
        }
        if len(mal_ids) != 1:
            raise SyncError(
                f"Kitsu {kitsu_id}: ожидался один MAL ID, найдено {sorted(mal_ids)}"
            )
        mal_id = mal_ids.pop()
        shikimori = await self.client.get_json(
            SHIKIMORI_ANIME_URL.format(mal_id=mal_id),
            headers={"Accept": "application/json"},
        )
        russian = str(shikimori.get("russian") or "").strip()
        if not russian:
            raise SyncError(f"MAL {mal_id}: у Shikimori отсутствует русское название")

        values = [
            russian,
            shikimori.get("name"),
            shikimori.get("license_name_ru"),
            *(shikimori.get("english") or []),
            *(shikimori.get("japanese") or []),
            *(shikimori.get("synonyms") or []),
        ]
        aliases = tuple(dict.fromkeys(str(value).strip() for value in values if value))
        return AnimeMetadata(kitsu_id, mal_id, russian, aliases)


def normalize_title(value: str) -> str:
    value = unicodedata.normalize("NFKC", value).casefold().replace("ё", "е")
    return re.sub(r"[^a-zа-я0-9]+", "", value)


class FansubsIndexer:
    def __init__(self, client: PoliteHttpClient):
        self.client = client

    async def find_exact_match(self, metadata: AnimeMetadata) -> FansubsCandidate:
        aliases = {normalize_title(alias) for alias in metadata.aliases}
        search_terms = tuple(
            dict.fromkeys(
                value
                for value in (metadata.russian_title, *metadata.aliases)
                if value and len(value.strip()) >= 2
            )
        )
        all_candidates: list[FansubsCandidate] = []
        for search_term in search_terms[:6]:
            # The legacy PHP form expects percent-encoded Windows-1251, not UTF-8.
            try:
                body = urlencode(
                    {"query": search_term}, encoding="cp1251"
                ).encode("ascii")
            except UnicodeEncodeError:
                continue
            payload, _ = await self.client.request(
                "POST",
                FANSUBS_SEARCH_URL,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                data=body,
            )
            soup = BeautifulSoup(payload.decode("cp1251", "replace"), "html.parser")
            term_candidates: list[FansubsCandidate] = []
            for link in soup.select('a[href^="base.php?id="]'):
                title_node = next(
                    (
                        child
                        for child in link.children
                        if isinstance(child, str) and child.strip()
                    ),
                    None,
                )
                title = str(
                    title_node or link.get_text(" ", strip=True)
                ).strip()
                candidate = FansubsCandidate(
                    title=title,
                    page_url=urljoin(FANSUBS_BASE_URL, link.get("href", "")),
                )
                if (
                    normalize_title(title) in aliases
                    and candidate not in term_candidates
                ):
                    term_candidates.append(candidate)
                if candidate not in all_candidates:
                    all_candidates.append(candidate)
            if len(term_candidates) == 1:
                return term_candidates[0]
            if len(term_candidates) > 1:
                raise AmbiguousMatchError(
                    "fansubs.ru: найдено несколько точных карточек: "
                    + ", ".join(item.page_url for item in term_candidates)
                )
        raise SyncError(
            "fansubs.ru: точное совпадение не найдено ни по одному алиасу "
            f"для {metadata.russian_title!r}; проверено {len(search_terms[:6])}"
        )

    async def discover_archives(self, page_url: str) -> tuple[FansubsArchive, ...]:
        payload, _ = await self.client.request("GET", page_url)
        soup = BeautifulSoup(payload.decode("cp1251", "replace"), "html.parser")
        ids = {
            int(field.get("value", ""))
            for field in soup.select('form[method="post" i] input[name="srt"][value]')
            if str(field.get("value", "")).isdigit()
        }
        if not ids:
            raise SyncError("В карточке fansubs.ru нет архивов")
        download_url = urljoin(page_url, "/base.php")
        return tuple(
            FansubsArchive(subtitle_id, download_url)
            for subtitle_id in sorted(ids)
        )

    async def download_archive(
        self, archive: FansubsArchive, destination: Path, max_bytes: int
    ) -> Path:
        body = urlencode({"srt": archive.subtitle_id}).encode("ascii")
        payload, headers = await self.client.request(
            "POST",
            archive.download_url,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            data=body,
            max_bytes=max_bytes,
        )
        disposition = str(headers.get("Content-Disposition", ""))
        match = re.search(r'filename="?([^";]+)', disposition, re.IGNORECASE)
        suffix = Path(match.group(1)).suffix.lower() if match else ""
        if suffix not in {".rar", ".zip", ".7z"}:
            if payload.startswith(b"Rar!"):
                suffix = ".rar"
            elif payload.startswith(b"PK\x03\x04"):
                suffix = ".zip"
            elif payload.startswith(b"7z\xbc\xaf\x27\x1c"):
                suffix = ".7z"
            else:
                raise SyncError("fansubs.ru вернул неизвестный формат архива")
        final_path = destination.with_suffix(suffix)
        final_path.write_bytes(payload)
        return final_path


class SyncWorker:
    def __init__(
        self,
        settings: Settings,
        client: PoliteHttpClient,
        storage: TelegramStorage,
    ):
        self.settings = settings
        self.metadata = MetadataResolver(client)
        self.fansubs = FansubsIndexer(client)
        self.storage = storage

    async def sync_one(
        self,
        kitsu_id: int,
        db: Session,
        source_page_url: str | None = None,
        source_fansubs_id: int | None = None,
        resolved_mal_id: int | None = None,
        resolved_title: str | None = None,
    ) -> SyncResult:
        record = db.get(SyncRecord, kitsu_id) or SyncRecord(kitsu_id=kitsu_id)
        record.last_attempt_at = utcnow()
        record.status = "running"
        db.add(record)
        db.commit()
        try:
            catalog_item = None
            if source_page_url is None:
                catalog_items = db.scalars(
                    select(FansubsCatalogItem).where(
                        FansubsCatalogItem.kitsu_id == kitsu_id,
                        FansubsCatalogItem.resolution_status == "resolved",
                    )
                ).all()
                if len(catalog_items) == 1:
                    catalog_item = catalog_items[0]
                    source_page_url = catalog_item.page_url
                    source_fansubs_id = catalog_item.fansubs_id
                    resolved_mal_id = catalog_item.mal_id
                    resolved_title = catalog_item.canonical_title
            metadata = (
                AnimeMetadata(
                    kitsu_id=kitsu_id,
                    mal_id=resolved_mal_id or 0,
                    russian_title=resolved_title or "",
                    aliases=tuple(filter(None, [resolved_title])),
                )
                if source_page_url
                else await self.metadata.resolve(kitsu_id)
            )
            record.mal_id = metadata.mal_id
            record.russian_title = metadata.russian_title
            candidate = (
                FansubsCandidate(title=metadata.russian_title, page_url=source_page_url)
                if source_page_url
                else await self.fansubs.find_exact_match(metadata)
            )
            record.fansubs_page_url = candidate.page_url
            imported = 0
            duplicates = 0
            unresolved: list[str] = []
            pending_asset_rows: list[dict] = []
            seen = {
                (asset.episode, asset.checksum)
                for asset in db.scalars(
                    select(SubtitleAsset).where(
                        SubtitleAsset.kitsu_id == kitsu_id
                    )
                ).all()
            }
            archives = await self.fansubs.discover_archives(candidate.page_url)

            async def ensure_storage_object(
                item,
                archive_id: int,
                episode_label: str,
            ) -> StorageObject:
                storage_object = db.scalar(
                    select(StorageObject).where(
                        StorageObject.backend == "telegram",
                        StorageObject.checksum == item.checksum,
                    )
                )
                if storage_object is not None:
                    return storage_object
                stored = await self.storage.upload(
                    item.path,
                    StorageMetadata(
                        filename=Path(item.original_name).name,
                        media_type=item.media_type,
                        checksum=item.checksum,
                        size_bytes=item.path.stat().st_size,
                        attributes={
                            "kitsu_id": str(kitsu_id),
                            "episode": episode_label,
                            "language": "rus",
                            "fansubs_title_id": str(
                                source_fansubs_id or "unknown"
                            ),
                            "fansubs_archive_id": str(archive_id),
                        },
                    ),
                )
                storage_object = StorageObject(
                    backend=stored.backend,
                    object_id=stored.object_id,
                    checksum=stored.checksum,
                    size_bytes=stored.size_bytes,
                    original_filename=stored.filename,
                    media_type=stored.media_type,
                    telegram_chat_id=stored.chat_id,
                    telegram_message_id=stored.message_id,
                    telegram_file_id=stored.file_id,
                    telegram_file_unique_id=stored.file_unique_id,
                )
                db.add(storage_object)
                db.commit()
                return storage_object

            for archive in archives:
                with TemporaryDirectory(
                    prefix="anisubio-",
                    dir=self.settings.temp_dir,
                ) as temporary_directory:
                    temporary_root = Path(temporary_directory)
                    archive_path = await self.fansubs.download_archive(
                        archive,
                        temporary_root / uuid4().hex,
                        self.settings.max_archive_bytes,
                    )
                    extraction_dir = temporary_root / "extracted"
                    extracted = extract_subtitles(
                        archive_path,
                        extraction_dir,
                        self.settings,
                    )
                    mapped_items = [
                        (item, episode_from_filename(item.original_name))
                        for item in extracted
                    ]
                    if len(mapped_items) == 1 and mapped_items[0][1] is None:
                        mapped_items[0] = (mapped_items[0][0], 1)

                    for item, episode in mapped_items:
                        if episode is None:
                            unresolved.append(item.original_name)
                            storage_object = await ensure_storage_object(
                                item,
                                archive.subtitle_id,
                                "unknown",
                            )
                            existing_issue = db.scalar(
                                select(UnresolvedSubtitle).where(
                                    UnresolvedSubtitle.kitsu_id == kitsu_id,
                                    UnresolvedSubtitle.checksum == item.checksum,
                                    UnresolvedSubtitle.fansubs_archive_id
                                    == archive.subtitle_id,
                                )
                            )
                            if existing_issue is None:
                                issue = UnresolvedSubtitle(
                                        kitsu_id=kitsu_id,
                                        fansubs_title_id=source_fansubs_id,
                                        fansubs_archive_id=archive.subtitle_id,
                                        original_filename=item.original_name,
                                        checksum=item.checksum,
                                        storage_object_id=storage_object.id,
                                    )
                                db.add(issue)
                                db.flush()
                                add_unresolved_subtitle(db, issue)
                                db.commit()
                            continue
                        identity = (episode, item.checksum)
                        if identity in seen:
                            duplicates += 1
                            continue

                        storage_object = await ensure_storage_object(
                            item,
                            archive.subtitle_id,
                            str(episode),
                        )

                        seen.add(identity)
                        pending_asset_rows.append(
                            {
                                "kitsu_id": kitsu_id,
                                "fansubs_id": source_fansubs_id,
                                "episode": episode,
                                "language": "rus",
                                "display_name": Path(item.original_name).stem,
                                "original_filename": item.original_name,
                                "stored_filename": None,
                                "media_type": item.media_type,
                                "checksum": item.checksum,
                                "storage_object_id": storage_object.id,
                                "source_url": (
                                    f"{candidate.page_url}#srt={archive.subtitle_id}"
                                ),
                            }
                        )
                        imported += 1

            db.add_all(SubtitleAsset(**row) for row in pending_asset_rows)
            record.status = (
                "success"
                if imported > 0 or duplicates > 0
                else "unresolved"
            )
            record.detail = (
                f"imported={imported}; duplicates={duplicates}; "
                f"unresolved={len(unresolved)}"
                + (
                    "; files=" + " | ".join(unresolved[:10])
                    if unresolved
                    else ""
                )
            )
            if record.status == "success":
                record.last_success_at = utcnow()
            db.add(record)
            db.commit()
            return SyncResult(
                kitsu_id,
                record.status,
                imported,
                duplicates,
                tuple(unresolved),
                record.detail,
            )
        except Exception as exc:
            db.rollback()
            record = db.get(SyncRecord, kitsu_id) or SyncRecord(kitsu_id=kitsu_id)
            record.status = "failed"
            record.detail = str(exc)
            record.last_attempt_at = utcnow()
            db.add(record)
            db.commit()
            return SyncResult(kitsu_id, "failed", detail=str(exc))
async def run_once(kitsu_ids: tuple[int, ...], settings: Settings) -> list[SyncResult]:
    settings.ensure_directories()
    create_schema()
    async with PoliteHttpClient(
        settings.sync_min_request_delay_seconds,
        settings.sync_max_request_delay_seconds,
        settings.request_timeout_seconds,
        proxy_url=settings.metadata_proxy_url,
        retries=settings.sync_request_retries,
        rate_limit_file=settings.http_rate_limit_file,
    ) as client:
        worker = SyncWorker(settings, client, create_telegram_storage(settings))
        with SessionLocal() as db:
            results = []
            for kitsu_id in kitsu_ids:
                result = await worker.sync_one(kitsu_id, db)
                LOG.info("Kitsu %s: %s — %s", kitsu_id, result.status, result.detail)
                results.append(result)
            return results


async def run_forever(
    kitsu_ids: tuple[int, ...],
    settings: Settings,
    queue: str = "all",
) -> None:
    settings.ensure_directories()
    create_schema()
    worker_id = f"{socket.gethostname()}:{os.getpid()}"
    next_vacuum_at = 0.0
    async with PoliteHttpClient(
        settings.sync_min_request_delay_seconds,
        settings.sync_max_request_delay_seconds,
        settings.request_timeout_seconds,
        proxy_url=settings.metadata_proxy_url,
        retries=settings.sync_request_retries,
        rate_limit_file=settings.http_rate_limit_file,
    ) as client:
        worker = SyncWorker(settings, client, create_telegram_storage(settings))
        recovery_min_priority = 100 if queue == "vacuum" else None
        recovery_max_priority = 99 if queue == "lazy" else None
        with SessionLocal() as db:
            recovered = recover_interrupted_jobs(
                db,
                min_priority=recovery_min_priority,
                max_priority=recovery_max_priority,
            )
            if recovered:
                LOG.warning("Recovered %s interrupted jobs", recovered)
        while True:
            now = time.monotonic()
            with SessionLocal() as db:
                if now >= next_vacuum_at:
                    for kitsu_id in kitsu_ids:
                        enqueue_sync_job(db, kitsu_id, reason="vacuum")
                    next_vacuum_at = now + settings.sync_interval_seconds

                min_priority = 100 if queue == "vacuum" else None
                max_priority = 99 if queue == "lazy" else None
                job = claim_next_job(
                    db,
                    worker_id,
                    min_priority=min_priority,
                    max_priority=max_priority,
                )
                if job is None:
                    await asyncio.sleep(settings.job_poll_interval_seconds)
                    continue
                result = await worker.sync_one(
                    job.kitsu_id,
                    db,
                    source_page_url=job.source_page_url,
                    source_fansubs_id=job.fansubs_id,
                    resolved_mal_id=job.resolved_mal_id,
                    resolved_title=job.resolved_title,
                )
                job = db.get(SyncJob, job.id)
                if job is None:
                    continue
                if result.status == "success":
                    complete_job(db, job)
                    LOG.info(
                        "Job %s completed: kitsu=%s imported=%s duplicates=%s",
                        job.id,
                        job.kitsu_id,
                        result.imported,
                        result.duplicates,
                    )
                elif result.status == "unresolved":
                    fail_job(
                        db,
                        job,
                        result.detail,
                        max_attempts=job.attempts,
                    )
                    LOG.warning(
                        "Job %s unresolved: kitsu=%s detail=%s",
                        job.id,
                        job.kitsu_id,
                        result.detail,
                    )
                else:
                    fail_job(
                        db,
                        job,
                        result.detail,
                        settings.job_max_attempts,
                    )
                    LOG.warning(
                        "Job %s failed: kitsu=%s detail=%s",
                        job.id,
                        job.kitsu_id,
                        result.detail,
                    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="AniSubio background sync worker")
    parser.add_argument(
        "--kitsu-id",
        type=int,
        action="append",
        dest="kitsu_ids",
        help="Kitsu ID; можно передать несколько раз",
    )
    parser.add_argument(
        "--queue",
        choices=("all", "lazy", "vacuum"),
        default="all",
        help="Выбирать все, только lazy-load или только vacuum jobs",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Выполнить один цикл и завершиться",
    )
    return parser.parse_args()


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    args = parse_args()
    settings = get_settings()
    kitsu_ids = tuple(args.kitsu_ids or settings.sync_kitsu_ids)
    if args.once and not kitsu_ids:
        raise SystemExit(
            "Не заданы Kitsu ID: используйте --kitsu-id или ANISUBIO_SYNC_KITSU_IDS"
        )
    if args.once:
        results = asyncio.run(run_once(kitsu_ids, settings))
        if any(result.status != "success" for result in results):
            raise SystemExit(1)
    else:
        asyncio.run(run_forever(kitsu_ids, settings, queue=args.queue))


if __name__ == "__main__":
    main()

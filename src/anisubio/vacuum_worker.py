from __future__ import annotations

import argparse
import asyncio
import logging
from datetime import datetime, timezone

from anisubio.config import Settings, get_settings
from anisubio.db import SessionLocal, create_schema
from anisubio.models import VacuumState
from anisubio.services.catalog import (
    FansubsCatalogCrawler,
    resolve_catalog_batch,
)
from anisubio.sync_worker import PoliteHttpClient


LOG = logging.getLogger("anisubio.vacuum")


def _scan_due(settings: Settings) -> bool:
    with SessionLocal() as db:
        state = db.get(VacuumState, "catalog_last_scan")
        if state is None:
            return True
        try:
            previous = datetime.fromisoformat(state.value)
        except ValueError:
            return True
        if previous.tzinfo is None:
            previous = previous.replace(tzinfo=timezone.utc)
        age = datetime.now(timezone.utc) - previous
        return age.total_seconds() >= settings.catalog_scan_interval_seconds


async def vacuum_cycle(settings: Settings) -> tuple[int, int]:
    create_schema()
    async with PoliteHttpClient(
        settings.sync_min_request_delay_seconds,
        settings.sync_max_request_delay_seconds,
        settings.request_timeout_seconds,
        proxy_url=settings.metadata_proxy_url,
        retries=settings.sync_request_retries,
        rate_limit_file=settings.http_rate_limit_file,
    ) as client:
        scanned = 0
        with SessionLocal() as db:
            if _scan_due(settings):
                scanned = await FansubsCatalogCrawler(client).refresh_catalog(db)
            resolved = await resolve_catalog_batch(
                db,
                client,
                settings.catalog_resolve_batch_size,
            )
        return scanned, resolved


async def run_forever(settings: Settings) -> None:
    settings.ensure_directories()
    while True:
        try:
            scanned, resolved = await vacuum_cycle(settings)
            LOG.info("Catalog scanned=%s resolved=%s", scanned, resolved)
        except Exception:
            LOG.exception("Vacuum cycle failed")
        await asyncio.sleep(settings.job_poll_interval_seconds)


def main() -> None:
    parser = argparse.ArgumentParser(description="AniSubio catalog vacuum")
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    settings = get_settings()
    if args.once:
        print(asyncio.run(vacuum_cycle(settings)))
    else:
        asyncio.run(run_forever(settings))


if __name__ == "__main__":
    main()

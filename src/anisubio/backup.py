from __future__ import annotations

import argparse
import asyncio
import gzip
import hashlib
import shutil
import sqlite3
from pathlib import Path
from tempfile import TemporaryDirectory

from anisubio.config import Settings, get_settings
from anisubio.db import SessionLocal, create_schema
from anisubio.models import DatabaseBackup, StorageObject, utcnow
from anisubio.storage import StorageMetadata
from anisubio.storage.factory import create_telegram_storage


def _snapshot_sqlite(source: Path, destination: Path) -> None:
    with sqlite3.connect(source) as source_db:
        with sqlite3.connect(destination) as destination_db:
            source_db.backup(destination_db)


async def backup_db(settings: Settings | None = None) -> DatabaseBackup:
    settings = settings or get_settings()
    settings.ensure_directories()
    create_schema()
    source = settings.sqlite_path
    if not source.is_file():
        raise FileNotFoundError(source)

    with TemporaryDirectory(
        prefix="anisubio-backup-",
        dir=settings.temp_dir,
    ) as temporary_directory:
        root = Path(temporary_directory)
        snapshot = root / "anisubio.db"
        compressed = root / f"anisubio-{utcnow():%Y%m%d-%H%M%S}.db.gz"
        _snapshot_sqlite(source, snapshot)
        with snapshot.open("rb") as input_file, gzip.open(
            compressed, "wb", compresslevel=6
        ) as output_file:
            shutil.copyfileobj(input_file, output_file)

        digest = hashlib.sha256(compressed.read_bytes()).hexdigest()
        storage = create_telegram_storage(settings, backup=True)
        stored = await storage.upload(
            compressed,
            StorageMetadata(
                filename=compressed.name,
                media_type="application/gzip",
                checksum=digest,
                size_bytes=compressed.stat().st_size,
                attributes={"kind": "sqlite-backup"},
            ),
        )

        with SessionLocal() as db:
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
            db.flush()
            backup = DatabaseBackup(
                storage_object_id=storage_object.id,
                checksum=digest,
                size_bytes=compressed.stat().st_size,
            )
            db.add(backup)
            db.commit()
            db.refresh(backup)
            return backup


async def backup_loop(settings: Settings) -> None:
    while True:
        try:
            await backup_db(settings)
        except Exception:
            import logging

            logging.getLogger("anisubio.backup").exception("Database backup failed")
        await asyncio.sleep(settings.backup_interval_seconds)


def main() -> None:
    parser = argparse.ArgumentParser(description="Backup AniSubio SQLite database")
    parser.add_argument("--loop", action="store_true")
    args = parser.parse_args()
    settings = get_settings()
    if args.loop:
        asyncio.run(backup_loop(settings))
    else:
        asyncio.run(backup_db(settings))


if __name__ == "__main__":
    main()

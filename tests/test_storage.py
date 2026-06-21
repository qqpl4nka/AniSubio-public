import hashlib
from pathlib import Path

import pytest

from anisubio.storage import (
    LocalCache,
    StorageMetadata,
    StoredObject,
    TelegramStorage,
)


class FakeTelegramTransport:
    def __init__(self):
        self.uploads = []

    async def upload_document(self, channel_id, file_path, caption):
        self.uploads.append((channel_id, file_path, caption))
        checksum = hashlib.sha256(file_path.read_bytes()).hexdigest()
        return StoredObject(
            backend="telegram",
            object_id="bot-file-id",
            checksum=checksum,
            size_bytes=file_path.stat().st_size,
            filename=file_path.name,
            media_type="text/x-ssa",
            message_id=42,
            chat_id=channel_id,
            file_id="bot-file-id",
            file_unique_id="unique-id",
        )

    async def get_file_url(self, object_id):
        return f"https://telegram.invalid/{object_id}"


def test_local_cache_round_trip(tmp_path: Path) -> None:
    source = tmp_path / "episode.ass"
    source.write_bytes(b"[Script Info]\n")
    checksum = hashlib.sha256(source.read_bytes()).hexdigest()
    cache = LocalCache(tmp_path / "cache", max_bytes=1024)

    stored = cache.put(source, checksum, ".ass")

    assert stored.read_bytes() == source.read_bytes()
    assert cache.get(checksum) == stored


def test_local_cache_rejects_wrong_checksum(tmp_path: Path) -> None:
    source = tmp_path / "episode.srt"
    source.write_text("subtitle", encoding="utf-8")
    cache = LocalCache(tmp_path / "cache")

    with pytest.raises(ValueError, match="checksum"):
        cache.put(source, "0" * 64, ".srt")


@pytest.mark.anyio
async def test_telegram_storage_upload_and_url(tmp_path: Path) -> None:
    source = tmp_path / "episode.ass"
    source.write_bytes(b"[Script Info]\n")
    checksum = hashlib.sha256(source.read_bytes()).hexdigest()
    transport = FakeTelegramTransport()
    storage = TelegramStorage(
        transport,
        channel_id=-100123,
        upload_interval_seconds=1,
    )
    metadata = StorageMetadata(
        filename=source.name,
        media_type="text/x-ssa",
        checksum=checksum,
        size_bytes=source.stat().st_size,
    )

    stored = await storage.upload(source, metadata)

    assert stored.message_id == 42
    assert stored.file_id == "bot-file-id"
    assert transport.uploads[0][1].name.startswith(
        "kitsu-unknown-eunknown-rus-"
    )
    assert "original_filename=episode.ass" in transport.uploads[0][2]
    assert await storage.get_stream_url(stored.object_id) == (
        "https://telegram.invalid/bot-file-id"
    )

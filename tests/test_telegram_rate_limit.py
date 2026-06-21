from pathlib import Path

import pytest

from anisubio.storage import StorageMetadata, StoredObject, TelegramStorage
from anisubio.storage.telegram import TelegramFloodWait


class FloodOnceTransport:
    def __init__(self):
        self.calls = 0

    async def upload_document(self, channel_id, file_path, caption):
        self.calls += 1
        if self.calls == 1:
            raise TelegramFloodWait(0, "retry")
        checksum = caption.split("sha256=", 1)[1].splitlines()[0]
        return StoredObject(
            backend="telegram",
            object_id="file-id",
            checksum=checksum,
            size_bytes=file_path.stat().st_size,
            filename=file_path.name,
            media_type="text/plain",
        )

    async def get_file_url(self, object_id):
        return "https://example.invalid"


@pytest.mark.anyio
async def test_telegram_storage_retries_flood_wait(tmp_path: Path) -> None:
    source = tmp_path / "episode.srt"
    source.write_text("subtitle", encoding="utf-8")
    transport = FloodOnceTransport()
    storage = TelegramStorage(transport, -1001, upload_interval_seconds=1)

    stored = await storage.upload(
        source,
        StorageMetadata(
            filename=source.name,
            media_type="application/x-subrip",
            checksum="a" * 64,
            size_bytes=source.stat().st_size,
        ),
    )

    assert stored.object_id == "file-id"
    assert transport.calls == 2

from __future__ import annotations

import asyncio
import json
import re
import time
from pathlib import Path
from typing import Protocol

import aiohttp
from aiohttp import FormData
from aiohttp_socks import ProxyConnector
from filelock import FileLock

from anisubio.storage.base import StorageBackend, StorageMetadata, StoredObject


class TelegramStorageError(RuntimeError):
    pass


class TelegramFloodWait(TelegramStorageError):
    def __init__(self, retry_after: int, detail: str):
        super().__init__(detail)
        self.retry_after = retry_after


class TelegramTransport(Protocol):
    """Runtime Telegram adapter.

    The production implementation may use Bot API or MTProto. The Codex MCP
    plugin administers channels, but is deliberately not a runtime dependency.
    """

    async def upload_document(
        self,
        channel_id: int,
        file_path: Path,
        caption: str,
    ) -> StoredObject: ...

    async def get_file_url(self, object_id: str) -> str: ...


class BotApiTransport:
    """Telegram Bot API transport routed through a SOCKS5 proxy."""

    def __init__(
        self,
        token: str,
        proxy_url: str,
        api_base: str = "https://api.telegram.org",
        timeout_seconds: float = 60.0,
    ):
        if not token:
            raise ValueError("TELEGRAM_BOT_TOKEN is required")
        self.token = token
        self.proxy_url = proxy_url
        self.api_base = api_base.rstrip("/")
        self.timeout = aiohttp.ClientTimeout(total=timeout_seconds)

    def _method_url(self, method: str) -> str:
        return f"{self.api_base}/bot{self.token}/{method}"

    def _file_url(self, file_path: str) -> str:
        return f"{self.api_base}/file/bot{self.token}/{file_path}"

    def _connector(self) -> ProxyConnector | aiohttp.TCPConnector:
        if self.proxy_url:
            return ProxyConnector.from_url(self.proxy_url)
        return aiohttp.TCPConnector()

    async def _json_request(
        self,
        method: str,
        *,
        data: FormData | dict[str, str] | None = None,
    ) -> dict:
        async with aiohttp.ClientSession(
            connector=self._connector(),
            timeout=self.timeout,
        ) as session:
            async with session.post(self._method_url(method), data=data) as response:
                payload = await response.read()
        try:
            decoded = json.loads(payload)
        except (UnicodeDecodeError, ValueError) as exc:
            raise TelegramStorageError(
                f"Telegram {method} returned invalid JSON"
            ) from exc
        if not decoded.get("ok"):
            retry_after = (
                decoded.get("parameters", {}).get("retry_after")
                if isinstance(decoded, dict)
                else None
            )
            detail = decoded.get("description", "unknown Telegram error")
            if retry_after:
                raise TelegramFloodWait(
                    int(retry_after),
                    f"Telegram flood limit: retry after {retry_after}s: {detail}",
                )
            raise TelegramStorageError(f"Telegram {method} failed: {detail}")
        return decoded["result"]

    async def upload_document(
        self,
        channel_id: int,
        file_path: Path,
        caption: str,
    ) -> StoredObject:
        form = FormData()
        file_handle = file_path.open("rb")
        form.add_field("chat_id", str(channel_id))
        form.add_field("caption", caption)
        form.add_field(
            "document",
            file_handle,
            filename=file_path.name,
            content_type="application/octet-stream",
        )
        try:
            message = await self._json_request("sendDocument", data=form)
        finally:
            file_handle.close()
        document = message.get("document") or {}
        file_id = document.get("file_id")
        if not file_id:
            raise TelegramStorageError("Telegram response has no document file_id")
        checksum = ""
        for line in caption.splitlines():
            if line.startswith("sha256="):
                checksum = line.removeprefix("sha256=")
                break
        return StoredObject(
            backend="telegram",
            object_id=file_id,
            checksum=checksum,
            size_bytes=int(document.get("file_size") or file_path.stat().st_size),
            filename=document.get("file_name") or file_path.name,
            media_type=document.get("mime_type") or "application/octet-stream",
            message_id=int(message["message_id"]),
            chat_id=int(message["chat"]["id"]),
            file_id=file_id,
            file_unique_id=document.get("file_unique_id"),
        )

    async def get_file_url(self, object_id: str) -> str:
        result = await self._json_request("getFile", data={"file_id": object_id})
        file_path = result.get("file_path")
        if not file_path:
            raise TelegramStorageError("Telegram getFile returned no file_path")
        return self._file_url(file_path)

    async def download_to(self, object_id: str, destination: Path) -> Path:
        url = await self.get_file_url(object_id)
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_suffix(destination.suffix + ".part")
        async with aiohttp.ClientSession(
            connector=self._connector(),
            timeout=self.timeout,
        ) as session:
            async with session.get(url) as response:
                response.raise_for_status()
                with temporary.open("wb") as output:
                    async for chunk in response.content.iter_chunked(64 * 1024):
                        output.write(chunk)
        temporary.replace(destination)
        return destination


class TelegramStorage(StorageBackend):
    name = "telegram"

    def __init__(
        self,
        transport: TelegramTransport,
        channel_id: int,
        upload_interval_seconds: float = 1.0,
        rate_limit_file: Path | None = None,
    ):
        self.transport = transport
        self.channel_id = channel_id
        self.upload_interval_seconds = max(upload_interval_seconds, 1.0)
        self.rate_limit_file = rate_limit_file
        self._upload_lock = asyncio.Lock()
        self._last_upload_at: float | None = None

    def _reserve_global_upload_slot(self) -> float:
        if self.rate_limit_file is None:
            return 0.0
        self.rate_limit_file.parent.mkdir(parents=True, exist_ok=True)
        lock = FileLock(str(self.rate_limit_file) + ".lock")
        with lock:
            now = time.time()
            last_upload_at = 0.0
            if self.rate_limit_file.is_file():
                try:
                    last_upload_at = float(
                        self.rate_limit_file.read_text(encoding="ascii").strip()
                    )
                except (OSError, ValueError):
                    last_upload_at = 0.0
            wait_seconds = max(
                0.0,
                self.upload_interval_seconds - (now - last_upload_at),
            )
            if wait_seconds == 0:
                self.rate_limit_file.write_text(
                    f"{now:.6f}",
                    encoding="ascii",
                )
            return wait_seconds

    async def _wait_for_global_upload_slot(self) -> None:
        if self.rate_limit_file is None:
            return
        while True:
            wait_seconds = await asyncio.to_thread(
                self._reserve_global_upload_slot
            )
            if wait_seconds <= 0:
                return
            await asyncio.sleep(wait_seconds)

    async def upload(
        self,
        file_path: Path,
        metadata: StorageMetadata,
    ) -> StoredObject:
        path = file_path.resolve()
        if not path.is_file():
            raise FileNotFoundError(path)
        if path.stat().st_size != metadata.size_bytes:
            raise ValueError("file size does not match storage metadata")

        attributes = {
            str(key): str(value)
            for key, value in metadata.attributes.items()
        }
        kitsu_id = attributes.get("kitsu_id", "unknown")
        episode = attributes.get("episode", "unknown")
        language = attributes.get("language", "rus")
        fansubs_title_id = attributes.get("fansubs_title_id", "unknown")
        fansubs_archive_id = attributes.get("fansubs_archive_id", "unknown")
        original_name = metadata.filename.replace("\n", " ").replace("\r", " ")
        safe_language = re.sub(r"[^a-z0-9_-]", "-", language.casefold())
        safe_episode = (
            f"{int(episode):04d}" if episode.isdigit() else "unknown"
        )
        suffix = Path(metadata.filename).suffix.lower()
        upload_name = (
            f"kitsu-{kitsu_id}-e{safe_episode}-{safe_language}-"
            f"{metadata.checksum[:16]}{suffix}"
        )

        async with self._upload_lock:
            if self._last_upload_at is not None:
                remaining = self.upload_interval_seconds - (
                    time.monotonic() - self._last_upload_at
                )
                if remaining > 0:
                    await asyncio.sleep(remaining)
            caption = (
                f"AniSubio Storage v1\n"
                f"kitsu_id={kitsu_id}\n"
                f"episode={episode}\n"
                f"language={language}\n"
                f"fansubs_title_id={fansubs_title_id}\n"
                f"fansubs_archive_id={fansubs_archive_id}\n"
                f"original_filename={original_name}\n"
                f"sha256={metadata.checksum}\n"
                f"type={metadata.media_type}"
            )
            for attempt in range(5):
                try:
                    await self._wait_for_global_upload_slot()
                    stored = await self.transport.upload_document(
                        self.channel_id,
                        _NamedPath(path, upload_name),
                        caption,
                    )
                    break
                except TelegramFloodWait as exc:
                    if attempt == 4:
                        raise
                    await asyncio.sleep(exc.retry_after + 1)
            else:
                raise TelegramStorageError("Telegram upload retry loop exhausted")
            self._last_upload_at = time.monotonic()

        if stored.backend != self.name:
            raise ValueError("Telegram transport returned another backend")
        if stored.checksum != metadata.checksum:
            raise ValueError("Telegram transport returned another checksum")
        return StoredObject(
            backend=stored.backend,
            object_id=stored.object_id,
            checksum=metadata.checksum,
            size_bytes=metadata.size_bytes,
            filename=metadata.filename,
            media_type=metadata.media_type,
            message_id=stored.message_id,
            chat_id=stored.chat_id,
            file_id=stored.file_id,
            file_unique_id=stored.file_unique_id,
        )

    async def get_stream_url(self, object_id: str) -> str:
        return await self.transport.get_file_url(object_id)


class _NamedPath:
    """Path-like upload wrapper with a human-readable Telegram filename."""

    def __init__(self, source: Path, name: str):
        self._source = source
        self.name = name

    def resolve(self) -> "_NamedPath":
        return self

    def is_file(self) -> bool:
        return self._source.is_file()

    def stat(self):
        return self._source.stat()

    def open(self, *args, **kwargs):
        return self._source.open(*args, **kwargs)

    def read_bytes(self) -> bytes:
        return self._source.read_bytes()

    def __fspath__(self) -> str:
        return str(self._source)

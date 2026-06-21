from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping


@dataclass(frozen=True, slots=True)
class StorageMetadata:
    filename: str
    media_type: str
    checksum: str
    size_bytes: int
    attributes: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class StoredObject:
    backend: str
    object_id: str
    checksum: str
    size_bytes: int
    filename: str
    media_type: str
    message_id: int | None = None
    chat_id: int | None = None
    file_id: str | None = None
    file_unique_id: str | None = None


class StorageBackend(ABC):
    """Immutable object storage used by workers and the FastAPI proxy."""

    name: str

    @abstractmethod
    async def upload(
        self,
        file_path: Path,
        metadata: StorageMetadata,
    ) -> StoredObject:
        """Upload one file and return its durable storage identifiers."""

    @abstractmethod
    async def get_stream_url(self, object_id: str) -> str:
        """Return a short-lived URL suitable for server-side streaming."""

    async def delete(self, object_id: str) -> None:
        """Delete an object when supported.

        Backends are immutable by default, and deletion is intentionally
        optional because Telegram storage may use append-only channels.
        """
        raise NotImplementedError(f"{self.name} does not support deletion")

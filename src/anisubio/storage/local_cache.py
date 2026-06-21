from __future__ import annotations

import asyncio
import hashlib
import os
import shutil
from pathlib import Path


class LocalCache:
    """Content-addressed hot cache with atomic writes."""

    def __init__(self, root: Path, max_bytes: int = 2 * 1024 * 1024 * 1024):
        self.root = root
        self.max_bytes = max_bytes
        self.root.mkdir(parents=True, exist_ok=True)
        self._locks: dict[str, asyncio.Lock] = {}
        self._locks_guard = asyncio.Lock()

    def path_for(self, checksum: str, suffix: str = "") -> Path:
        if len(checksum) != 64 or any(c not in "0123456789abcdef" for c in checksum):
            raise ValueError("checksum must be a lowercase SHA-256 hex digest")
        normalized_suffix = suffix.lower()
        if normalized_suffix and (
            not normalized_suffix.startswith(".")
            or any(char in normalized_suffix for char in ("/", "\\"))
        ):
            raise ValueError("invalid cache suffix")
        return self.root / checksum[:2] / f"{checksum}{normalized_suffix}"

    def get(self, checksum: str) -> Path | None:
        directory = self.root / checksum[:2]
        matches = list(directory.glob(f"{checksum}.*")) if directory.is_dir() else []
        if not matches:
            bare = directory / checksum
            matches = [bare] if bare.is_file() else []
        if not matches:
            return None
        path = matches[0]
        os.utime(path, None)
        return path

    async def lock_for(self, key: str) -> asyncio.Lock:
        async with self._locks_guard:
            return self._locks.setdefault(key, asyncio.Lock())

    def put(
        self,
        source: Path,
        checksum: str,
        suffix: str = "",
    ) -> Path:
        destination = self.path_for(checksum, suffix)
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.is_file():
            os.utime(destination, None)
            return destination
        temporary = destination.with_suffix(destination.suffix + ".part")
        digest = hashlib.sha256()
        with source.open("rb") as input_file, temporary.open("wb") as output_file:
            while chunk := input_file.read(1024 * 1024):
                digest.update(chunk)
                output_file.write(chunk)
            output_file.flush()
            os.fsync(output_file.fileno())
        if digest.hexdigest() != checksum:
            temporary.unlink(missing_ok=True)
            raise ValueError("source checksum does not match cache key")
        temporary.replace(destination)
        self.prune()
        return destination

    def prune(self) -> None:
        files = [path for path in self.root.rglob("*") if path.is_file()]
        total = sum(path.stat().st_size for path in files)
        if total <= self.max_bytes:
            return
        for path in sorted(files, key=lambda item: item.stat().st_atime):
            size = path.stat().st_size
            path.unlink(missing_ok=True)
            total -= size
            if total <= self.max_bytes:
                break

    def clear(self) -> None:
        for child in self.root.iterdir():
            if child.is_dir():
                shutil.rmtree(child)
            else:
                child.unlink(missing_ok=True)

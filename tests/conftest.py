from pathlib import Path

import pytest

from anisubio.config import Settings


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        _env_file=None,
        public_base_url="https://example.test",
        database_url=f"sqlite:///{tmp_path / 'test.db'}",
        storage_dir=tmp_path / "subtitles",
        download_dir=tmp_path / "downloads",
        max_archive_bytes=1024 * 1024,
        max_extracted_bytes=1024 * 1024,
        max_archive_files=100,
    )

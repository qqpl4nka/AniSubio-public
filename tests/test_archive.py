import zipfile
from pathlib import Path

import pytest
import py7zr

from anisubio.services.archive import ArchiveError, extract_subtitles


def test_extracts_only_subtitles(settings, tmp_path: Path) -> None:
    archive_path = tmp_path / "season.zip"
    settings.ensure_directories()
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr("Anime - 01.ass", "[Script Info]\n")
        archive.writestr("readme.txt", "ignored")

    files = extract_subtitles(archive_path, settings.storage_dir, settings)

    assert len(files) == 1
    assert files[0].original_name == "Anime - 01.ass"
    assert files[0].path.is_file()


def test_rejects_path_traversal(settings, tmp_path: Path) -> None:
    archive_path = tmp_path / "bad.zip"
    settings.ensure_directories()
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr("../escape.ass", "bad")

    with pytest.raises(ArchiveError, match="Небезопасный путь"):
        extract_subtitles(archive_path, settings.storage_dir, settings)


def test_extracts_7z_subtitles(settings, tmp_path: Path) -> None:
    source = tmp_path / "Anime - 03.ass"
    source.write_text("[Script Info]\n", encoding="utf-8")
    archive_path = tmp_path / "season.7z"
    settings.ensure_directories()
    with py7zr.SevenZipFile(archive_path, "w") as archive:
        archive.write(source, arcname=source.name)

    files = extract_subtitles(archive_path, settings.storage_dir, settings)

    assert len(files) == 1
    assert files[0].original_name == "Anime - 03.ass"
    assert files[0].path.read_text(encoding="utf-8") == "[Script Info]\n"

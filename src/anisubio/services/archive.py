import hashlib
import re
import shutil
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

import httpx
import py7zr
import rarfile

from anisubio.config import Settings


SUBTITLE_SUFFIXES = {".ass", ".ssa", ".srt", ".vtt"}
MEDIA_TYPES = {
    ".ass": "text/x-ssa",
    ".ssa": "text/x-ssa",
    ".srt": "application/x-subrip",
    ".vtt": "text/vtt",
}


class ArchiveError(RuntimeError):
    pass


@dataclass(frozen=True)
class ExtractedSubtitle:
    original_name: str
    path: Path
    checksum: str
    media_type: str


def _safe_member_name(name: str) -> PurePosixPath:
    normalized = PurePosixPath(name.replace("\\", "/"))
    if normalized.is_absolute() or ".." in normalized.parts:
        raise ArchiveError(f"Небезопасный путь в архиве: {name}")
    return normalized


async def download_archive(url: str, destination: Path, settings: Settings) -> Path:
    return await download_archive_request(url, destination, settings)


async def download_archive_request(
    url: str,
    destination: Path,
    settings: Settings,
    form_data: dict[str, str] | None = None,
) -> Path:
    headers = {"User-Agent": settings.http_user_agent}
    size = 0
    async with httpx.AsyncClient(
        headers=headers,
        timeout=settings.request_timeout_seconds,
        follow_redirects=True,
    ) as client:
        request = client.build_request(
            "POST" if form_data else "GET",
            url,
            data=form_data,
        )
        async with client.stream(request.method, request.url, data=form_data) as response:
            response.raise_for_status()
            declared = int(response.headers.get("content-length", "0") or 0)
            if declared > settings.max_archive_bytes:
                raise ArchiveError("Архив превышает разрешённый размер")
            with destination.open("wb") as output:
                async for chunk in response.aiter_bytes():
                    size += len(chunk)
                    if size > settings.max_archive_bytes:
                        raise ArchiveError("Архив превышает разрешённый размер")
                    output.write(chunk)
            disposition = response.headers.get("content-disposition", "")
            match = re.search(r'filename="?([^";]+)', disposition, re.IGNORECASE)
            if match:
                suffix = Path(match.group(1)).suffix.lower()
                if suffix in {".zip", ".rar", ".7z"} and destination.suffix.lower() != suffix:
                    renamed = destination.with_suffix(suffix)
                    destination.replace(renamed)
                    destination = renamed
    return destination


def _store_file(
    source, original_name: str, storage_dir: Path, extracted_size: int
) -> ExtractedSubtitle:
    safe_name = _safe_member_name(original_name)
    suffix = safe_name.suffix.lower()
    digest = hashlib.sha256()
    temp_path = storage_dir / f".import-{hashlib.sha1(original_name.encode()).hexdigest()}"
    written = 0
    with temp_path.open("wb") as output:
        while chunk := source.read(1024 * 1024):
            written += len(chunk)
            if written > extracted_size:
                temp_path.unlink(missing_ok=True)
                raise ArchiveError("Файл превышает разрешённый распакованный размер")
            digest.update(chunk)
            output.write(chunk)
    checksum = digest.hexdigest()
    final_name = f"{checksum}{suffix}"
    final_path = storage_dir / final_name
    if final_path.exists():
        temp_path.unlink(missing_ok=True)
    else:
        shutil.move(temp_path, final_path)
    return ExtractedSubtitle(
        original_name=str(safe_name),
        path=final_path,
        checksum=checksum,
        media_type=MEDIA_TYPES[suffix],
    )


def extract_subtitles(
    archive_path: Path, storage_dir: Path, settings: Settings
) -> list[ExtractedSubtitle]:
    storage_dir.mkdir(parents=True, exist_ok=True)
    suffix = archive_path.suffix.lower()
    extracted: list[ExtractedSubtitle] = []
    total_size = 0

    if suffix == ".zip":
        with zipfile.ZipFile(archive_path) as archive:
            members = [item for item in archive.infolist() if not item.is_dir()]
            if len(members) > settings.max_archive_files:
                raise ArchiveError("В архиве слишком много файлов")
            for member in members:
                safe_name = _safe_member_name(member.filename)
                if safe_name.suffix.lower() not in SUBTITLE_SUFFIXES:
                    continue
                total_size += member.file_size
                if total_size > settings.max_extracted_bytes:
                    raise ArchiveError("Архив превышает лимит после распаковки")
                with archive.open(member) as source:
                    extracted.append(
                        _store_file(
                            source,
                            member.filename,
                            storage_dir,
                            settings.max_extracted_bytes,
                        )
                    )
    elif suffix == ".rar":
        try:
            with rarfile.RarFile(archive_path) as archive:
                members = [item for item in archive.infolist() if not item.isdir()]
                if len(members) > settings.max_archive_files:
                    raise ArchiveError("В архиве слишком много файлов")
                for member in members:
                    safe_name = _safe_member_name(member.filename)
                    if safe_name.suffix.lower() not in SUBTITLE_SUFFIXES:
                        continue
                    total_size += member.file_size
                    if total_size > settings.max_extracted_bytes:
                        raise ArchiveError("Архив превышает лимит после распаковки")
                    with archive.open(member) as source:
                        extracted.append(
                            _store_file(
                                source,
                                member.filename,
                                storage_dir,
                                settings.max_extracted_bytes,
                            )
                        )
        except rarfile.RarCannotExec as exc:
            raise ArchiveError(
                "Для RAR требуется 7z, unrar или bsdtar в PATH"
            ) from exc
    elif suffix == ".7z":
        staging_dir = storage_dir / f".7z-{archive_path.stem}"
        staging_dir.mkdir(parents=True, exist_ok=True)
        try:
            with py7zr.SevenZipFile(archive_path, mode="r") as archive:
                members = archive.list()
                files = [item for item in members if not item.is_directory]
                if len(files) > settings.max_archive_files:
                    raise ArchiveError("В архиве слишком много файлов")
                targets: list[str] = []
                for member in files:
                    safe_name = _safe_member_name(member.filename)
                    if safe_name.suffix.lower() not in SUBTITLE_SUFFIXES:
                        continue
                    member_size = int(member.uncompressed or 0)
                    total_size += member_size
                    if total_size > settings.max_extracted_bytes:
                        raise ArchiveError("Архив превышает лимит после распаковки")
                    targets.append(member.filename)
                if not targets:
                    raise ArchiveError(
                        "В архиве нет поддерживаемых файлов субтитров"
                    )
                archive.extract(path=staging_dir, targets=targets)

            for original_name in targets:
                safe_name = _safe_member_name(original_name)
                extracted_path = staging_dir.joinpath(*safe_name.parts)
                if not extracted_path.is_file():
                    raise ArchiveError(
                        f"Распакованный файл отсутствует: {original_name}"
                    )
                with extracted_path.open("rb") as source:
                    extracted.append(
                        _store_file(
                            source,
                            original_name,
                            storage_dir,
                            settings.max_extracted_bytes,
                        )
                    )
        finally:
            shutil.rmtree(staging_dir, ignore_errors=True)
    else:
        raise ArchiveError("Поддерживаются только ZIP, RAR и 7z")

    if not extracted:
        raise ArchiveError("В архиве нет поддерживаемых файлов субтитров")
    return extracted

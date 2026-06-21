from pathlib import Path
import re
from urllib.parse import urlparse
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from anisubio.config import Settings
from anisubio.models import SubtitleAsset
from anisubio.schemas import ImportRequest, ImportResult
from anisubio.services.archive import download_archive_request, extract_subtitles
from anisubio.services.mapper import episode_from_filename
from anisubio.services.source import FansubsSource


async def import_subtitles(
    request: ImportRequest, db: Session, settings: Settings
) -> ImportResult:
    source = FansubsSource(settings)
    form_data: dict[str, str] | None = None
    if request.archive_url:
        archive_url = str(request.archive_url)
        source.validate_url(archive_url)
        source_reference = archive_url
        suffix = Path(urlparse(archive_url).path).suffix.lower()
    else:
        download = await source.discover_download(
            str(request.source_page_url),
            request.fansubs_subtitle_id,
        )
        archive_url = download.download_url
        source_reference = f"{download.page_url}#srt={download.subtitle_id}"
        form_data = {"srt": str(download.subtitle_id)}
        suffix = ".archive"

    archive_path = settings.download_dir / f"{uuid4().hex}{suffix}"
    archive_path = await download_archive_request(
        archive_url, archive_path, settings, form_data=form_data
    )
    try:
        files = extract_subtitles(archive_path, settings.storage_dir, settings)
    finally:
        archive_path.unlink(missing_ok=True)

    imported = 0
    duplicates = 0
    unresolved: list[str] = []
    episodes: set[int] = set()
    fansubs_match = re.search(r"[?&]id=(\d+)", source_reference)
    fansubs_id = int(fansubs_match.group(1)) if fansubs_match else None

    for item in files:
        episode = episode_from_filename(
            item.original_name, request.filename_episode_offset
        )
        if episode is None:
            unresolved.append(item.original_name)
            continue

        existing = db.scalar(
            select(SubtitleAsset).where(SubtitleAsset.checksum == item.checksum)
        )
        if existing:
            duplicates += 1
            continue

        db.add(
            SubtitleAsset(
                kitsu_id=request.kitsu_id,
                fansubs_id=fansubs_id,
                episode=episode,
                language=request.language,
                display_name=Path(item.original_name).stem,
                original_filename=item.original_name,
                stored_filename=item.path.name,
                media_type=item.media_type,
                checksum=item.checksum,
                source_url=source_reference,
            )
        )
        imported += 1
        episodes.add(episode)

    db.commit()
    return ImportResult(
        archive_url=source_reference,
        imported=imported,
        duplicates=duplicates,
        unresolved_files=unresolved,
        imported_episodes=sorted(episodes),
    )

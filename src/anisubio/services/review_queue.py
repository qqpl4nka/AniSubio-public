from __future__ import annotations

import json

from sqlalchemy import select
from sqlalchemy.orm import Session

from anisubio.models import ReviewItem, SyncJob, UnresolvedSubtitle


def classify_failure(error: str | None) -> str:
    text = (error or "").casefold()
    if not text:
        return "unknown_error"
    if "нет архивов" in text:
        return "no_archives"
    if "нет поддерживаемых файлов" in text:
        return "no_subtitle_files"
    if any(value in text for value in ("read enough data", "crc", "corrupt")):
        return "broken_archive"
    if "unresolved=" in text:
        return "unmapped_filenames"
    if "404" in text:
        return "metadata_not_found"
    if "точное совпадение" in text or "несколько точных карточек" in text:
        return "title_mapping"
    return "import_error"


def add_failed_job(db: Session, job: SyncJob) -> ReviewItem:
    key = f"sync_job:{job.id}"
    item = db.scalar(select(ReviewItem).where(ReviewItem.dedupe_key == key))
    if item is None:
        item = ReviewItem(
            dedupe_key=key,
            item_type="sync_job",
            sync_job_id=job.id,
            kitsu_id=job.kitsu_id,
        )
    item.category = classify_failure(job.error)
    item.fansubs_id = job.fansubs_id
    item.source_url = job.source_page_url
    item.summary = job.error or "Exception did not contain a message"
    item.attempts = job.attempts
    item.payload_json = json.dumps(
        {
            "reason": job.reason,
            "requested_episode": job.requested_episode,
            "resolved_mal_id": job.resolved_mal_id,
            "resolved_title": job.resolved_title,
        },
        ensure_ascii=False,
    )
    db.add(item)
    return item


def add_unresolved_subtitle(
    db: Session,
    unresolved: UnresolvedSubtitle,
) -> ReviewItem:
    key = f"unresolved_subtitle:{unresolved.id}"
    item = db.scalar(select(ReviewItem).where(ReviewItem.dedupe_key == key))
    if item is None:
        item = ReviewItem(
            dedupe_key=key,
            item_type="subtitle_file",
            unresolved_subtitle_id=unresolved.id,
            kitsu_id=unresolved.kitsu_id,
        )
    item.category = "unmapped_filename"
    item.fansubs_id = unresolved.fansubs_title_id
    item.summary = unresolved.original_filename
    item.payload_json = json.dumps(
        {
            "fansubs_archive_id": unresolved.fansubs_archive_id,
            "checksum": unresolved.checksum,
            "storage_object_id": unresolved.storage_object_id,
            "reason": unresolved.reason,
        },
        ensure_ascii=False,
    )
    db.add(item)
    return item


def backfill_review_queue(db: Session) -> tuple[int, int]:
    failed_added = 0
    unresolved_added = 0
    for job in db.scalars(select(SyncJob).where(SyncJob.status == "failed")):
        if db.scalar(
            select(ReviewItem.id).where(
                ReviewItem.dedupe_key == f"sync_job:{job.id}"
            )
        ) is None:
            add_failed_job(db, job)
            failed_added += 1
    for unresolved in db.scalars(
        select(UnresolvedSubtitle).where(
            UnresolvedSubtitle.status == "pending_review"
        )
    ):
        if db.scalar(
            select(ReviewItem.id).where(
                ReviewItem.dedupe_key
                == f"unresolved_subtitle:{unresolved.id}"
            )
        ) is None:
            add_unresolved_subtitle(db, unresolved)
            unresolved_added += 1
    db.commit()
    return failed_added, unresolved_added

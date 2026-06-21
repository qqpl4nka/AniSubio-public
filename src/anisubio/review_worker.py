from __future__ import annotations

import argparse
import json
import logging
import time

from sqlalchemy import select
from sqlalchemy.orm import Session

from anisubio.config import Settings, get_settings
from anisubio.db import SessionLocal, create_schema
from anisubio.models import ReviewAnalysis, ReviewItem, UnresolvedSubtitle, utcnow
from anisubio.services.mapper import episode_from_filename


LOG = logging.getLogger("anisubio.review")
ANALYZER_VERSION = "1"

FAILURE_RECOMMENDATIONS = {
    "broken_archive": ("redownload_then_alternate_extractor", 85, True),
    "no_archives": ("recheck_source_after_first_pass", 90, True),
    "no_subtitle_files": ("inspect_archive_contents", 90, False),
    "metadata_not_found": ("verify_external_ids", 95, False),
    "title_mapping": ("manual_title_mapping", 90, False),
    "unknown_error": ("inspect_full_exception", 40, True),
    "import_error": ("classify_exception_then_retry", 50, True),
    "unmapped_filenames": ("analyze_subtitle_files", 95, False),
}


def analyze_item(db: Session, item: ReviewItem) -> ReviewAnalysis:
    recommendation = "manual_review"
    confidence = 30
    candidate_episode = None
    retryable = False
    evidence: dict[str, object] = {
        "item_type": item.item_type,
        "category": item.category,
        "summary": item.summary,
    }

    if item.item_type == "subtitle_file" and item.unresolved_subtitle_id:
        unresolved = db.get(UnresolvedSubtitle, item.unresolved_subtitle_id)
        if unresolved is not None:
            candidate_episode = episode_from_filename(unresolved.original_filename)
            evidence.update(
                {
                    "filename": unresolved.original_filename,
                    "fansubs_archive_id": unresolved.fansubs_archive_id,
                    "storage_object_id": unresolved.storage_object_id,
                }
            )
            if candidate_episode is not None:
                recommendation = "assign_detected_episode"
                confidence = 98
            else:
                siblings = db.scalars(
                    select(UnresolvedSubtitle).where(
                        UnresolvedSubtitle.kitsu_id == unresolved.kitsu_id,
                        UnresolvedSubtitle.fansubs_archive_id
                        == unresolved.fansubs_archive_id,
                    )
                ).all()
                evidence["archive_file_count"] = len(siblings)
                evidence["archive_filenames"] = [
                    sibling.original_filename for sibling in siblings[:50]
                ]
                recommendation = (
                    "analyze_archive_sequence"
                    if len(siblings) > 1
                    else "inspect_subtitle_metadata"
                )
                confidence = 65 if len(siblings) > 1 else 45
    else:
        recommendation, confidence, retryable = FAILURE_RECOMMENDATIONS.get(
            item.category,
            ("manual_review", 30, False),
        )
        try:
            evidence["original_payload"] = json.loads(item.payload_json or "{}")
        except ValueError:
            evidence["original_payload"] = item.payload_json

    analysis = db.scalar(
        select(ReviewAnalysis).where(
            ReviewAnalysis.review_item_id == item.id
        )
    )
    if analysis is None:
        analysis = ReviewAnalysis(review_item_id=item.id)
    analysis.analyzer_version = ANALYZER_VERSION
    analysis.recommendation = recommendation
    analysis.confidence_percent = confidence
    analysis.candidate_episode = candidate_episode
    analysis.retryable = int(retryable)
    analysis.evidence_json = json.dumps(evidence, ensure_ascii=False)
    analysis.analyzed_at = utcnow()
    db.add(analysis)
    return analysis


def analyze_batch(db: Session, batch_size: int) -> int:
    items = db.scalars(
        select(ReviewItem)
        .outerjoin(
            ReviewAnalysis,
            ReviewAnalysis.review_item_id == ReviewItem.id,
        )
        .where(
            ReviewItem.status == "pending_review",
            ReviewAnalysis.id.is_(None),
        )
        .order_by(ReviewItem.created_at)
        .limit(batch_size)
    ).all()
    for item in items:
        analyze_item(db, item)
    db.commit()
    return len(items)


def run_forever(settings: Settings) -> None:
    create_schema()
    while True:
        with SessionLocal() as db:
            analyzed = analyze_batch(db, settings.review_batch_size)
        if analyzed:
            LOG.info("Analyzed %s review items", analyzed)
        time.sleep(settings.review_poll_interval_seconds)


def main() -> None:
    parser = argparse.ArgumentParser(description="AniSubio review analyzer")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--batch-size", type=int)
    args = parser.parse_args()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    settings = get_settings()
    create_schema()
    if args.once:
        with SessionLocal() as db:
            print(analyze_batch(db, args.batch_size or settings.review_batch_size))
    else:
        run_forever(settings)


if __name__ == "__main__":
    main()

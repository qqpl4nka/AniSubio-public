from __future__ import annotations

import argparse
import json
import re

from sqlalchemy import delete, select

from anisubio.db import SessionLocal, create_schema
from anisubio.models import FansubsCatalogItem, ReviewItem, SubtitleAsset
from anisubio.services.mapper import episode_from_filename


SUSPICIOUS_EPISODES = {360, 480, 540, 576, 720, 1080, 1280, 1440, 1920, 2160, 4320}
MANUAL_CONTENT_MARKERS = re.compile(
    r"(?i)\b(?:ova|oad|oav|movie|special|picture\s*story|cross\s*over|"
    r"epilogue|safety\s*education)\b"
)
SEQUEL_MARKER = re.compile(r"(?i)\b(?:r|s|season|tv)[ ._-]*([2-9])\b")


def requires_manual_series_review(
    item: FansubsCatalogItem,
    filename: str,
) -> bool:
    if MANUAL_CONTENT_MARKERS.search(filename):
        return True
    # Aliases can contain multiple seasons from one mixed fansubs card, while
    # the catalog Kitsu ID represents only the canonical entry.
    source_names = item.canonical_title.casefold()
    return any(
        match.group(0).casefold() not in source_names
        for match in SEQUEL_MARKER.finditer(filename)
    )


def repair_catalog_outliers_in_session(
    db,
    *,
    apply: bool = False,
) -> dict[str, int]:
    """Correct only deterministic outliers and queue every ambiguous case."""
    result = {"examined": 0, "corrected": 0, "duplicates": 0, "reviewed": 0}
    rows = db.execute(
        select(SubtitleAsset, FansubsCatalogItem)
        .join(
            FansubsCatalogItem,
            FansubsCatalogItem.fansubs_id == SubtitleAsset.fansubs_id,
        )
        .where(
            FansubsCatalogItem.resolution_status == "resolved",
            FansubsCatalogItem.kitsu_id == SubtitleAsset.kitsu_id,
            FansubsCatalogItem.episode_count.is_not(None),
        )
    ).all()
    for asset, item in rows:
        if not item.episode_count or item.episode_count <= 0:
            continue
        tolerance = max(2, round(item.episode_count * 0.1))
        maximum_expected = item.episode_count + tolerance
        if asset.episode <= maximum_expected:
            continue
        result["examined"] += 1
        detected = episode_from_filename(asset.original_filename)
        if (
            detected is not None
            and detected != asset.episode
            and detected <= maximum_expected
            and not requires_manual_series_review(item, asset.original_filename)
        ):
            duplicate = db.scalar(
                select(SubtitleAsset).where(
                    SubtitleAsset.kitsu_id == asset.kitsu_id,
                    SubtitleAsset.episode == detected,
                    SubtitleAsset.storage_object_id == asset.storage_object_id,
                    SubtitleAsset.id != asset.id,
                )
            )
            if duplicate is not None:
                result["duplicates"] += 1
                if apply:
                    db.delete(asset)
            else:
                result["corrected"] += 1
                if apply:
                    asset.episode = detected
                    db.add(asset)
            continue

        result["reviewed"] += 1
        if apply:
            key = f"episode_outlier:{asset.id}"
            review = db.scalar(
                select(ReviewItem).where(ReviewItem.dedupe_key == key)
            )
            if review is None:
                db.add(
                    ReviewItem(
                        dedupe_key=key,
                        item_type="subtitle_asset",
                        category="episode_range_outlier",
                        kitsu_id=asset.kitsu_id,
                        fansubs_id=asset.fansubs_id,
                        source_url=asset.source_url,
                        summary=asset.original_filename,
                        payload_json=json.dumps(
                            {
                                "asset_id": asset.id,
                                "current_episode": asset.episode,
                                "detected_episode": detected,
                                "catalog_episode_count": item.episode_count,
                                "maximum_expected": maximum_expected,
                            },
                            ensure_ascii=False,
                        ),
                    )
                )
    if apply:
        db.commit()
    else:
        db.rollback()
    return result


def repair_catalog_outliers(*, apply: bool = False) -> dict[str, int]:
    with SessionLocal() as db:
        return repair_catalog_outliers_in_session(db, apply=apply)


def repair_suspicious_mappings(*, apply: bool = False) -> dict[str, int]:
    result = {"examined": 0, "corrected": 0, "duplicates": 0, "quarantined": 0}
    with SessionLocal() as db:
        assets = db.scalars(
            select(SubtitleAsset).where(
                SubtitleAsset.episode.in_(SUSPICIOUS_EPISODES)
            )
        ).all()
        for asset in assets:
            result["examined"] += 1
            detected = episode_from_filename(asset.original_filename)
            if detected is not None and detected not in SUSPICIOUS_EPISODES:
                duplicate = db.scalar(
                    select(SubtitleAsset).where(
                        SubtitleAsset.kitsu_id == asset.kitsu_id,
                        SubtitleAsset.episode == detected,
                        SubtitleAsset.storage_object_id == asset.storage_object_id,
                        SubtitleAsset.id != asset.id,
                    )
                )
                if duplicate is not None:
                    result["duplicates"] += 1
                    if apply:
                        db.delete(asset)
                else:
                    result["corrected"] += 1
                    if apply:
                        asset.episode = detected
                        db.add(asset)
                continue

            result["quarantined"] += 1
            if apply:
                key = f"suspicious_asset:{asset.id}"
                review = db.scalar(
                    select(ReviewItem).where(ReviewItem.dedupe_key == key)
                )
                if review is None:
                    fansubs_id = None
                    if asset.source_url:
                        match = re.search(r"[?&]id=(\d+)", asset.source_url)
                        fansubs_id = int(match.group(1)) if match else None
                    review = ReviewItem(
                        dedupe_key=key,
                        item_type="subtitle_asset",
                        category="false_episode_resolution",
                        kitsu_id=asset.kitsu_id,
                        fansubs_id=fansubs_id,
                        source_url=asset.source_url,
                        summary=asset.original_filename,
                        payload_json=json.dumps(
                            {
                                "asset_id": asset.id,
                                "old_episode": asset.episode,
                                "storage_object_id": asset.storage_object_id,
                            },
                            ensure_ascii=False,
                        ),
                    )
                    db.add(review)
                db.delete(asset)
        if apply:
            db.commit()
        else:
            db.rollback()
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="Repair false episode mappings")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--catalog-outliers", action="store_true")
    args = parser.parse_args()
    create_schema()
    operation = (
        repair_catalog_outliers
        if args.catalog_outliers
        else repair_suspicious_mappings
    )
    print(json.dumps(operation(apply=args.apply), indent=2))


if __name__ == "__main__":
    main()

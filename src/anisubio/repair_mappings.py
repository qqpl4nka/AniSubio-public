from __future__ import annotations

import argparse
import json
import re

from sqlalchemy import delete, select

from anisubio.db import SessionLocal, create_schema
from anisubio.models import ReviewItem, SubtitleAsset
from anisubio.services.mapper import episode_from_filename


SUSPICIOUS_EPISODES = {360, 480, 540, 576, 720, 1080, 1280, 1440, 1920, 2160, 4320}


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
    args = parser.parse_args()
    create_schema()
    print(json.dumps(repair_suspicious_mappings(apply=args.apply), indent=2))


if __name__ == "__main__":
    main()

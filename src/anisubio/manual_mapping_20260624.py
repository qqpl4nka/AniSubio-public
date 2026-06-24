from __future__ import annotations

import argparse
import json
import re

from sqlalchemy import select
from sqlalchemy.orm import Session

from anisubio.db import SessionLocal, create_schema
from anisubio.models import FansubsCatalogItem, ReviewItem, SubtitleAsset
from anisubio.services.mapper import episode_from_filename


# fansubs_id: (kitsu_id, mal_id, episode_count, start_year)
CATALOG_RESOLUTIONS = {
    828: (781, 881, 1, 2002),
    874: (406, 443, 1, 2002),
    875: (483, 524, 1, 2004),
    1849: (1544, 1723, 1, 2007),
    3594: (6474, 11209, 2, 2012),
    3646: (6412, 10934, 1, 2011),
    3774: (6411, 10933, 1, 2011),
    4012: (6801, 12729, 2, 2012),
    4179: (7318, 15819, 2, 2012),
    4198: (7279, 15591, 1, 2013),
    4359: (7430, 16444, 1, 2013),
    4482: (8055, 20745, 1, 2013),
    4486: (7690, 17855, 1, 2013),
    4716: (8251, 22057, 1, 2014),
    4721: (8730, 23441, 1, 2014),
    4730: (8142, 21679, 1, 2014),
    4805: (8560, 24171, 3, 2014),
    4941: (10055, 28713, 1, 2015),
    5116: (10143, 29027, 1, 2015),
    5285: (11402, 31704, 1, 2015),
    5452: (11186, 31156, 1, 2016),
    5538: (11342, 31378, 1, 2016),
    5614: (11836, 32698, 2, 2016),
}


# asset_id: (expected_fansubs_id, kitsu_id, episode)
MIXED_ASSET_MAPPINGS = {
    3049: (3543, 6397, 1),
    3050: (3543, 6397, 1),
    3051: (3543, 7069, 1),
    5231: (3746, 6492, 1),
    5232: (3746, 6492, 2),
    5233: (3746, 8185, 1),
    5234: (3746, 8185, 2),
    9724: (4575, 8152, 1),
    9725: (4575, 8152, 2),
    9726: (4575, 11243, 1),
    9727: (4575, 11243, 2),
    9728: (4575, 8152, 1),
    9729: (4575, 8152, 2),
    9730: (4575, 11243, 1),
    9731: (4575, 11243, 2),
    9732: (4575, 8152, 2),
}


def _asset(db: Session, asset_id: int, fansubs_id: int) -> SubtitleAsset:
    asset = db.get(SubtitleAsset, asset_id)
    if asset is None or asset.fansubs_id != fansubs_id:
        raise RuntimeError(
            f"Asset {asset_id} does not belong to fansubs {fansubs_id}"
        )
    return asset


def _set_asset(
    asset: SubtitleAsset,
    kitsu_id: int,
    episode: int,
    *,
    manual: bool,
) -> None:
    if episode <= 0:
        raise RuntimeError(f"Invalid episode {episode} for asset {asset.id}")
    asset.kitsu_id = kitsu_id
    asset.episode = episode
    asset.manual_verified = int(manual)
    asset.mapping_quarantined = 0


def _quarantine(db: Session, asset: SubtitleAsset, reason: str) -> None:
    asset.manual_verified = 0
    asset.mapping_quarantined = 1
    key = f"manual_mapping_quarantine:{asset.id}"
    review = db.scalar(select(ReviewItem).where(ReviewItem.dedupe_key == key))
    if review is None:
        review = ReviewItem(
            dedupe_key=key,
            item_type="subtitle_asset",
            category="manual_mapping_quarantine",
            kitsu_id=asset.kitsu_id,
            fansubs_id=asset.fansubs_id,
            source_url=asset.source_url,
            summary=reason,
            payload_json=json.dumps(
                {
                    "asset_id": asset.id,
                    "filename": asset.original_filename,
                    "reason": reason,
                },
                ensure_ascii=False,
            ),
        )
        db.add(review)


def _resolve_catalog_sources(db: Session) -> int:
    changed = 0
    for fansubs_id, (kitsu_id, mal_id, episode_count, year) in (
        CATALOG_RESOLUTIONS.items()
    ):
        item = db.get(FansubsCatalogItem, fansubs_id)
        if item is None:
            raise RuntimeError(f"Missing fansubs catalog item {fansubs_id}")
        item.kitsu_id = kitsu_id
        item.mal_id = mal_id
        item.episode_count = episode_count
        item.start_year = year
        item.resolution_status = "resolved"
        item.resolution_detail = (
            "Manual audit 2026-06-24: independently verified "
            f"Kitsu {kitsu_id} / MAL {mal_id}"
        )
        assets = db.scalars(
            select(SubtitleAsset).where(SubtitleAsset.fansubs_id == fansubs_id)
        ).all()
        if not assets:
            raise RuntimeError(f"No assets for fansubs item {fansubs_id}")
        for asset in assets:
            detected = episode_from_filename(asset.original_filename)
            episode = (
                1
                if episode_count == 1
                else detected
                if detected is not None and detected <= episode_count
                else asset.episode
                if asset.episode <= episode_count
                else 1
            )
            _set_asset(asset, kitsu_id, episode, manual=False)
            changed += 1
    return changed


def _resolve_mixed_sources(db: Session) -> int:
    for asset_id, (fansubs_id, kitsu_id, episode) in MIXED_ASSET_MAPPINGS.items():
        _set_asset(
            _asset(db, asset_id, fansubs_id),
            kitsu_id,
            episode,
            manual=True,
        )
    return len(MIXED_ASSET_MAPPINGS)


def _resolve_remaining_outliers(db: Session) -> tuple[int, int]:
    changed = 0
    quarantined = 0

    # Oreimo season 1 contains web specials and animated commentaries.
    for asset in db.scalars(
        select(SubtitleAsset).where(SubtitleAsset.fansubs_id == 3112)
    ):
        lowered = asset.original_filename.casefold()
        if re.search(r"animated[ _.-]+commentary", lowered):
            match = re.search(r"(?i)commentary[_ .-]*(\d{1,2})", lowered)
            episode = int(match.group(1)) if match else 0
            if 1 <= episode <= 16:
                _set_asset(asset, 6037, episode, manual=True)
                changed += 1
            else:
                _quarantine(db, asset, "Unresolved Oreimo animated commentary")
                quarantined += 1
        elif "12.5" in lowered:
            _quarantine(db, asset, "Oreimo fractional bonus episode")
            quarantined += 1
        elif 13 <= asset.episode <= 16:
            _set_asset(asset, 5998, asset.episode - 12, manual=True)
            changed += 1

    # Magic Knight Rayearth II is stored as episodes 21-49 in the same card.
    for asset in db.scalars(
        select(SubtitleAsset).where(
            SubtitleAsset.fansubs_id == 234,
            SubtitleAsset.kitsu_id == 399,
            SubtitleAsset.episode > 20,
        )
    ):
        _set_asset(asset, 1403, asset.episode - 20, manual=True)
        changed += 1

    # Code Geass card is R2; picture dramas are a separate Kitsu entry.
    for asset in db.scalars(
        select(SubtitleAsset).where(SubtitleAsset.fansubs_id == 1880)
    ):
        name = asset.original_filename
        lowered = name.casefold()
        if "picture drama" in lowered or "picture_drama" in lowered:
            match = re.search(
                r"(?i)picture[ _]drama[^0-9]{0,4}(\d{1,2})",
                name,
            )
            episode = int(match.group(1)) if match else 0
            if 1 <= episode <= 9:
                _set_asset(asset, 3960, episode, manual=True)
                changed += 1
            else:
                _quarantine(db, asset, "Unresolved Code Geass R2 picture drama")
                quarantined += 1
        elif "sp stage" in lowered:
            _quarantine(db, asset, "Unresolved Code Geass stage special")
            quarantined += 1
        else:
            episode = episode_from_filename(name)
            if episode is None or episode > 25:
                _quarantine(db, asset, "Unresolved Code Geass R2 episode")
                quarantined += 1
            else:
                _set_asset(asset, 2634, episode, manual=False)
                changed += 1

    # White Album 2nd Season is numbered 14-26 in legacy releases.
    for asset in db.scalars(
        select(SubtitleAsset).where(SubtitleAsset.fansubs_id == 2630)
    ):
        episode = episode_from_filename(asset.original_filename)
        if "white album ii" in asset.original_filename.casefold():
            if episode is not None and 1 <= episode <= 13:
                _set_asset(asset, 7697, episode, manual=True)
                changed += 1
            else:
                _quarantine(db, asset, "Unresolved White Album 2 episode")
                quarantined += 1
        elif episode is not None and 14 <= episode <= 26:
            _set_asset(asset, 4455, episode - 13, manual=False)
            changed += 1
        else:
            _quarantine(db, asset, "Unresolved White Album source")
            quarantined += 1

    # Bonus tracks with no reliable episode identity remain stored but hidden.
    for asset_id in (30468, 30487, 53126, 53415):
        asset = db.get(SubtitleAsset, asset_id)
        if asset is not None:
            _quarantine(db, asset, "Bonus content lacks a reliable episode identity")
            quarantined += 1

    # Oreimo 2 web specials continue numbering as episodes 14-16.
    for asset in db.scalars(
        select(SubtitleAsset).where(
            SubtitleAsset.fansubs_id == 4222,
            SubtitleAsset.episode >= 14,
        )
    ):
        _set_asset(asset, 7822, asset.episode - 13, manual=True)
        changed += 1

    _set_asset(_asset(db, 48645, 5033), 10923, 1, manual=True)
    changed += 1

    for asset_id, kitsu_id, episode in (
        (53123, 10965, 1),
        (53124, 10965, 2),
        (53125, 41516, 1),
        (53127, 10169, 1),
    ):
        _set_asset(_asset(db, asset_id, 5166), kitsu_id, episode, manual=True)
        changed += 1

    # Hataraku Maou-sama season 2 part 2 continues legacy numbering at 13.
    for asset in db.scalars(
        select(SubtitleAsset).where(
            SubtitleAsset.fansubs_id == 6923,
            SubtitleAsset.episode > 12,
        )
    ):
        _set_asset(asset, 46550, asset.episode - 12, manual=True)
        changed += 1
    return changed, quarantined


def apply_manual_audit(db: Session, *, apply: bool = False) -> dict[str, int]:
    result = {
        "catalog_assets": _resolve_catalog_sources(db),
        "mixed_assets": _resolve_mixed_sources(db),
    }
    result["remaining_assets"], result["quarantined"] = (
        _resolve_remaining_outliers(db)
    )
    if apply:
        db.commit()
    else:
        db.rollback()
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="Apply reviewed mapping audit")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    create_schema()
    with SessionLocal() as db:
        print(
            json.dumps(
                apply_manual_audit(db, apply=args.apply),
                ensure_ascii=False,
                indent=2,
            )
        )


if __name__ == "__main__":
    main()

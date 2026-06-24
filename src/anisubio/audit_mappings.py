from __future__ import annotations

import argparse
import asyncio
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Awaitable, Callable

from sqlalchemy import select
from sqlalchemy.orm import Session

from anisubio.db import SessionLocal
from anisubio.models import ExternalIdMapping, FansubsCatalogItem, SubtitleAsset
from anisubio.services.catalog import normalize_title
from anisubio.services.external_ids import CINEMETA_META_URL, _get_json


JsonFetcher = Callable[[str], Awaitable[dict]]


@dataclass(frozen=True)
class MappingFinding:
    severity: str
    code: str
    entity_type: str
    entity_id: str
    evidence: dict[str, object]


def _aliases(item: FansubsCatalogItem) -> set[str]:
    try:
        values = json.loads(item.aliases_json or "[]")
    except (json.JSONDecodeError, TypeError):
        values = []
    return {
        normalized
        for value in (item.canonical_title, *values)
        if (normalized := normalize_title(str(value)))
    }


def audit_database(db: Session) -> list[MappingFinding]:
    """Inspect mapping invariants without modifying the database."""
    findings: list[MappingFinding] = []
    items = db.scalars(select(FansubsCatalogItem)).all()
    assets = db.scalars(select(SubtitleAsset)).all()
    catalog = {item.fansubs_id: item for item in items}

    by_fansubs: dict[int, list[SubtitleAsset]] = defaultdict(list)
    for asset in assets:
        if asset.mapping_quarantined:
            continue
        if asset.episode <= 0:
            findings.append(
                MappingFinding(
                    "critical",
                    "invalid_episode_number",
                    "subtitle_asset",
                    str(asset.id),
                    {"episode": asset.episode, "kitsu_id": asset.kitsu_id},
                )
            )
        if asset.fansubs_id is not None:
            by_fansubs[asset.fansubs_id].append(asset)

    for fansubs_id, source_assets in sorted(by_fansubs.items()):
        item = catalog.get(fansubs_id)
        actual_kitsu_ids = sorted({asset.kitsu_id for asset in source_assets})
        catalog_assets = [
            asset for asset in source_assets if not asset.manual_verified
        ]
        if not catalog_assets:
            continue
        catalog_kitsu_ids = sorted({asset.kitsu_id for asset in catalog_assets})
        if item is None:
            findings.append(
                MappingFinding(
                    "critical",
                    "missing_catalog_provenance",
                    "fansubs_item",
                    str(fansubs_id),
                    {
                        "asset_count": len(catalog_assets),
                        "actual_kitsu_ids": catalog_kitsu_ids,
                    },
                )
            )
            continue
        if item.resolution_status != "resolved" or item.kitsu_id is None:
            findings.append(
                MappingFinding(
                    "critical",
                    "asset_source_not_resolved",
                    "fansubs_item",
                    str(fansubs_id),
                    {
                        "asset_count": len(catalog_assets),
                        "resolution_status": item.resolution_status,
                        "catalog_kitsu_id": item.kitsu_id,
                        "actual_kitsu_ids": catalog_kitsu_ids,
                    },
                )
            )
            continue
        wrong_kitsu_ids = [
            kitsu_id for kitsu_id in catalog_kitsu_ids if kitsu_id != item.kitsu_id
        ]
        if wrong_kitsu_ids:
            findings.append(
                MappingFinding(
                    "critical",
                    "asset_catalog_kitsu_mismatch",
                    "fansubs_item",
                    str(fansubs_id),
                    {
                        "asset_count": len(catalog_assets),
                        "catalog_kitsu_id": item.kitsu_id,
                        "actual_kitsu_ids": catalog_kitsu_ids,
                        "wrong_kitsu_ids": wrong_kitsu_ids,
                    },
                )
            )
        if len(catalog_kitsu_ids) > 1:
            findings.append(
                MappingFinding(
                    "critical",
                    "archive_split_across_kitsu",
                    "fansubs_item",
                    str(fansubs_id),
                    {
                        "asset_count": len(catalog_assets),
                        "actual_kitsu_ids": catalog_kitsu_ids,
                    },
                )
            )
        if item.episode_count and item.episode_count > 0:
            maximum_episode = max(asset.episode for asset in catalog_assets)
            tolerance = max(2, round(item.episode_count * 0.1))
            if maximum_episode > item.episode_count + tolerance:
                findings.append(
                    MappingFinding(
                        "high",
                        "episode_range_outlier",
                        "fansubs_item",
                        str(fansubs_id),
                        {
                            "catalog_episode_count": item.episode_count,
                            "maximum_asset_episode": maximum_episode,
                            "tolerance": tolerance,
                            "kitsu_id": item.kitsu_id,
                        },
                    )
                )

    mal_to_kitsu: dict[int, set[int]] = defaultdict(set)
    kitsu_to_mal: dict[int, set[int]] = defaultdict(set)
    for item in items:
        if (
            item.resolution_status == "resolved"
            and item.mal_id is not None
            and item.kitsu_id is not None
        ):
            mal_to_kitsu[item.mal_id].add(item.kitsu_id)
            kitsu_to_mal[item.kitsu_id].add(item.mal_id)
    for mal_id, kitsu_ids in sorted(mal_to_kitsu.items()):
        if len(kitsu_ids) > 1:
            findings.append(
                MappingFinding(
                    "critical",
                    "mal_maps_multiple_kitsu",
                    "mal_id",
                    str(mal_id),
                    {"kitsu_ids": sorted(kitsu_ids)},
                )
            )
    for kitsu_id, mal_ids in sorted(kitsu_to_mal.items()):
        if len(mal_ids) > 1:
            findings.append(
                MappingFinding(
                    "critical",
                    "kitsu_maps_multiple_mal",
                    "kitsu_id",
                    str(kitsu_id),
                    {"mal_ids": sorted(mal_ids)},
                )
            )
    return findings


async def audit_external_mappings(
    db: Session,
    fetch_json: JsonFetcher = _get_json,
) -> list[MappingFinding]:
    """Validate cached IMDb mappings against Cinemeta and catalog titles."""
    findings: list[MappingFinding] = []
    items = db.scalars(
        select(FansubsCatalogItem).where(
            FansubsCatalogItem.resolution_status == "resolved",
            FansubsCatalogItem.kitsu_id.is_not(None),
        )
    ).all()
    by_kitsu: dict[int, list[FansubsCatalogItem]] = defaultdict(list)
    title_to_kitsu: dict[str, set[int]] = defaultdict(set)
    for item in items:
        if item.kitsu_id is None:
            continue
        by_kitsu[item.kitsu_id].append(item)
        for title in _aliases(item):
            title_to_kitsu[title].add(item.kitsu_id)

    mappings = db.scalars(select(ExternalIdMapping)).all()
    for mapping in mappings:
        entity_id = f"{mapping.external_id}:{mapping.season}"
        try:
            payload = await fetch_json(
                CINEMETA_META_URL.format(imdb_id=mapping.external_id)
            )
        except Exception as exc:
            findings.append(
                MappingFinding(
                    "medium",
                    "external_metadata_unavailable",
                    "external_mapping",
                    entity_id,
                    {"error_type": type(exc).__name__},
                )
            )
            continue
        title = str(payload.get("meta", {}).get("name") or "")
        normalized = normalize_title(title)
        exact_candidates = sorted(title_to_kitsu.get(normalized, set()))
        mapped_titles = {
            value
            for item in by_kitsu.get(mapping.kitsu_id, [])
            for value in _aliases(item)
        }
        if exact_candidates and mapping.kitsu_id not in exact_candidates:
            findings.append(
                MappingFinding(
                    "critical",
                    "external_title_maps_other_kitsu",
                    "external_mapping",
                    entity_id,
                    {
                        "cinemeta_title": title,
                        "mapped_kitsu_id": mapping.kitsu_id,
                        "exact_catalog_kitsu_ids": exact_candidates,
                        "tvdb_id": mapping.tvdb_id,
                    },
                )
            )
        elif normalized and normalized not in mapped_titles:
            findings.append(
                MappingFinding(
                    "high",
                    "external_title_not_in_mapped_catalog",
                    "external_mapping",
                    entity_id,
                    {
                        "cinemeta_title": title,
                        "mapped_kitsu_id": mapping.kitsu_id,
                        "tvdb_id": mapping.tvdb_id,
                    },
                )
            )
    return findings


def build_report(findings: list[MappingFinding]) -> dict[str, object]:
    severity_counts = Counter(finding.severity for finding in findings)
    code_counts = Counter(finding.code for finding in findings)
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "read_only": True,
        "summary": {
            "total_findings": len(findings),
            "by_severity": dict(sorted(severity_counts.items())),
            "by_code": dict(sorted(code_counts.items())),
        },
        "findings": [asdict(finding) for finding in findings],
    }


async def run_audit(online: bool) -> dict[str, object]:
    with SessionLocal() as db:
        findings = audit_database(db)
        if online:
            findings.extend(await audit_external_mappings(db))
    return build_report(findings)


def main() -> None:
    parser = argparse.ArgumentParser(description="Read-only AniSubio mapping audit")
    parser.add_argument("--online", action="store_true")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    report = asyncio.run(run_audit(args.online))
    payload = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(payload + "\n", encoding="utf-8")
    print(payload)


if __name__ == "__main__":
    main()

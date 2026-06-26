from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from anisubio.db import Base
from anisubio.main import (
    IMDB_VIDEO_ID,
    KITSU_VIDEO_ID,
    app,
    addon_manifest_url,
    is_auxiliary_subtitle,
    load_home_stats,
    load_subtitles_with_encoding_fallback,
    stremio_install_url,
    stremio_web_install_url,
)
from anisubio.models import FansubsCatalogItem, SubtitleAsset


def test_kitsu_episode_id() -> None:
    match = KITSU_VIDEO_ID.fullmatch("kitsu:11:1")
    assert match
    assert match.group("kitsu_id") == "11"
    assert match.group("episode") == "1"


def test_rejects_season_style_kitsu_id() -> None:
    assert KITSU_VIDEO_ID.fullmatch("kitsu:11:1:2") is None


def test_imdb_series_video_id() -> None:
    match = IMDB_VIDEO_ID.fullmatch("tt3114390:1:1")
    assert match
    assert match.group("imdb_id") == "tt3114390"
    assert match.group("season") == "1"
    assert match.group("episode") == "1"


def test_stremio_install_url_keeps_deployment_path() -> None:
    assert stremio_install_url(
        "https://example.test/anisubio"
    ) == "stremio://example.test/anisubio/v2/manifest.json"


def test_stremio_web_install_url_encodes_manifest() -> None:
    assert stremio_web_install_url(
        "https://example.test/anisubio"
    ) == (
        "https://web.stremio.com/#/addons?addon="
        "https%3A%2F%2Fexample.test%2Fanisubio%2Fv2%2Fmanifest.json"
    )


def test_versioned_manifest_url() -> None:
    assert addon_manifest_url(
        "https://example.test/anisubio"
    ) == "https://example.test/anisubio/v2/manifest.json"


def test_srt_file_routes_precede_integer_only_routes() -> None:
    paths = [route.path for route in app.routes]
    assert paths.index("/files/{asset_id}.{extension}") < paths.index(
        "/files/{asset_id}"
    )
    assert paths.index("/v2/files/{asset_id}.{extension}") < paths.index(
        "/v2/files/{asset_id}"
    )


def test_cp1251_subtitles_are_loaded_with_fallback(tmp_path: Path) -> None:
    source = tmp_path / "legacy.ssa"
    source.write_bytes(
        (
            "[Script Info]\nScriptType: v4.00+\n"
            "[V4+ Styles]\n"
            "Format: Name, Fontname, Fontsize, PrimaryColour, "
            "SecondaryColour, OutlineColour, BackColour, Bold, Italic, "
            "Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, "
            "BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, "
            "MarginV, Encoding\n"
            "Style: Default,Arial,20,&H00FFFFFF,&H000000FF,&H00000000,"
            "&H00000000,0,0,0,0,100,100,0,0,1,2,0,2,10,10,10,1\n"
            "[Events]\n"
            "Format: Layer, Start, End, Style, Name, MarginL, MarginR, "
            "MarginV, Effect, Text\n"
            "Dialogue: 0,0:00:01.00,0:00:02.00,Default,,0,0,0,,Привет\n"
        ).encode("cp1251")
    )

    subtitles = load_subtitles_with_encoding_fallback(source)

    assert subtitles[0].text == "Привет"


def test_auxiliary_tracks_are_detected_without_matching_regular_words() -> None:
    assert is_auxiliary_subtitle("Bleach Sennen Kessen Hen ED ep 01.ass")
    assert is_auxiliary_subtitle("[Group] Opening 01.srt")
    assert is_auxiliary_subtitle("episode 01 signs.ass")
    assert not is_auxiliary_subtitle("DogeEx Bleach episode 01.ass")
    assert not is_auxiliary_subtitle("Steins;Gate 01.ass")


def test_home_stats_count_only_served_inventory() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    with Session(engine) as db:
        db.add_all([
            FansubsCatalogItem(
                fansubs_id=1,
                page_url="http://fansubs.test/1",
                canonical_title="Resolved",
                kitsu_id=10,
                resolution_status="resolved",
            ),
            FansubsCatalogItem(
                fansubs_id=2,
                page_url="http://fansubs.test/2",
                canonical_title="Wrong mapping",
                kitsu_id=999,
                resolution_status="resolved",
            ),
        ])
        db.add_all([
            SubtitleAsset(
                kitsu_id=10,
                fansubs_id=1,
                episode=1,
                language="rus",
                display_name="valid",
                original_filename="valid.ass",
                media_type="text/x-ssa",
                checksum="a" * 64,
            ),
            SubtitleAsset(
                kitsu_id=10,
                fansubs_id=1,
                episode=2,
                language="rus",
                display_name="valid 2",
                original_filename="valid-2.ass",
                media_type="text/x-ssa",
                checksum="b" * 64,
            ),
            SubtitleAsset(
                kitsu_id=20,
                fansubs_id=2,
                episode=1,
                language="rus",
                display_name="mismatch",
                original_filename="mismatch.ass",
                media_type="text/x-ssa",
                checksum="c" * 64,
            ),
            SubtitleAsset(
                kitsu_id=30,
                fansubs_id=2,
                episode=1,
                language="rus",
                display_name="manual split",
                original_filename="manual.ass",
                media_type="text/x-ssa",
                checksum="d" * 64,
                manual_verified=1,
            ),
            SubtitleAsset(
                kitsu_id=40,
                episode=1,
                language="rus",
                display_name="quarantined",
                original_filename="quarantined.ass",
                media_type="text/x-ssa",
                checksum="e" * 64,
                mapping_quarantined=1,
            ),
        ])
        db.commit()

        stats = load_home_stats(db)

    assert stats["subtitle_count"] == 3
    assert stats["anime_count"] == 2
    assert stats["episode_count"] == 3
    assert stats["catalog_total"] == 2
    assert stats["catalog_resolved"] == 2
    assert stats["catalog_imported"] == 2

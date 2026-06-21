import pytest

from anisubio.services.mapper import episode_from_filename


@pytest.mark.parametrize(
    ("filename", "expected"),
    [
        ("Anime - 01.ass", 1),
        ("Anime [02].srt", 2),
        ("Anime.E03.1080p.ass", 3),
        ("Anime - Episode 004.vtt", 4),
        ("Anime - 01v2.ass", 1),
        ("Anime - 2x07.ass", 7),
        ("folder/Anime_12_[Group].ssa", 12),
        ("[ReinForce] Anime OVA1 (BDRip).ass", 1),
        ("Anime Special 02.ass", 2),
        ("Аниме серия №03.ass", 3),
        ("[Silent-Raws] Gakkatsu! - 13 (NHK-E 1280x720 x264 AAC).ass", 13),
        ("Anime - 24 [1080p x265].ass", 24),
    ],
)
def test_episode_from_filename(filename: str, expected: int) -> None:
    assert episode_from_filename(filename) == expected


def test_unknown_filename_is_unresolved() -> None:
    assert episode_from_filename("opening.ass") is None


def test_offset() -> None:
    assert episode_from_filename("Anime - 00.ass", offset=1) == 1

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
        ("[DB]_Naruto_191_[E7923CB9].srt", 191),
        ("[RAW] Galaxy Express 999 001.srt", 1),
        ("Code Geass R2 07 RAW (704x396 DivX6.82).ass", 7),
        ("Golgo 13 - 02 [ROOM No.909](1280x720 DivX682).ass", 2),
        ("Toward_the_Terra_05_[E5347D34].ass", 5),
        ("Campanella - 09 (1280x720 H.264 AAC).ass", 9),
        ("Mirai_Nikki_-_09_[E386B859].ass", 9),
        ("Show_by_Rock_-_10_RAW_MX_1280.ass", 10),
        ("Captain Harlock SSX - 21 [E887F320].ass", 21),
        ("Demon Slayer - 19 [E148A5FF].ass", 19),
        ("Summertime Render - 11 [1080p][E329A9D8].ass", 11),
    ],
)
def test_episode_from_filename(filename: str, expected: int) -> None:
    assert episode_from_filename(filename) == expected


def test_unknown_filename_is_unresolved() -> None:
    assert episode_from_filename("opening.ass") is None
    assert episode_from_filename("Clannad Movie (AC3 5.1).ass") is None
    assert episode_from_filename("Yamada CrossOver [BD 1080].ass") is None


def test_offset() -> None:
    assert episode_from_filename("Anime - 00.ass", offset=1) == 1

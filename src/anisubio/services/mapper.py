import re
from pathlib import PurePosixPath


EPISODE_PATTERNS = (
    re.compile(r"(?i)(?:^|[\s._\-\[\(])(?:ep(?:isode)?|e)\s*0*(\d{1,4})(?:v\d+)?(?:\D|$)"),
    re.compile(r"(?i)(?:^|[\s._\-\[\(])(?:ova|oad|special|sp)\s*0*(\d{1,4})(?:v\d+)?(?:\D|$)"),
    re.compile(r"(?i)(?:^|[\s._\-\[\(])(?:серия|эпизод)\s*№?\s*0*(\d{1,4})(?:\D|$)"),
    re.compile(r"(?i)(?:^|[\s._\-\[\(])\d{1,2}x0*(\d{1,4})(?:\D|$)"),
    re.compile(
        r"(?i)(?<![a-zа-я0-9])0*(\d{1,4})(?:v\d+)?"
        r"(?=$|[\s._\-\]\)])"
    ),
)

TECHNICAL_NUMBER_PATTERNS = (
    # Release checksums such as [E7923CB9] used to look like E7923.
    re.compile(r"(?i)\[(?=[a-f0-9]{6,10}\])(?=[a-f0-9]*[a-f])[a-f0-9]+\]"),
    re.compile(r"(?i)\b(?:[1-9]\d{2,3})\s*[xх×]\s*(?:[1-9]\d{2,3})\b"),
    re.compile(r"(?i)\b(?:360|480|540|576|720|1080|1440|2160|4320)p\b"),
    re.compile(r"(?i)\b[хxh]\.?(?:264|265|266)\b"),
    re.compile(r"(?i)\bdivx\s*\d+(?:\.\d+)+\b"),
    re.compile(r"(?i)\b(?:8|10|12)[- ]?bit\b"),
    re.compile(r"(?i)\b(?:2|5|7)\.1(?:ch)?\b"),
    re.compile(r"(?i)\b(?:19|20)\d{2}\b"),
    re.compile(r"(?i)(?:^|[\s._\-\[(])(?:bd|dvd|web)[- _]?(?:360|480|540|576|720|1080|1440|2160|4320)\b"),
    re.compile(r"(?i)(?:^|[\s._\-\[(])(?:mx|tv|raw)[- _]+(?:360|480|540|576|720|1080|1280|1440|1920|2160|4320)\b"),
    re.compile(r"(?i)\broom\s+no\.?\s*\d+\b"),
)


def episode_from_filename(filename: str, offset: int = 0) -> int | None:
    stem = PurePosixPath(filename.replace("\\", "/")).stem
    for technical_pattern in TECHNICAL_NUMBER_PATTERNS:
        stem = technical_pattern.sub(" ", stem)
    for pattern in EPISODE_PATTERNS:
        matches = list(pattern.finditer(stem))
        if not matches:
            continue
        episode = int(matches[-1].group(1)) + offset
        if episode > 0:
            return episode
    return None

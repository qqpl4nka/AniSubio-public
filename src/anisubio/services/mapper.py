import re
from pathlib import PurePosixPath


EPISODE_PATTERNS = (
    re.compile(r"(?i)(?:^|[\s._\-\[\(])(?:ep(?:isode)?|e)\s*0*(\d{1,4})(?:v\d+)?(?:\D|$)"),
    re.compile(r"(?i)(?:^|[\s._\-\[\(])(?:ova|oad|special|sp)\s*0*(\d{1,4})(?:v\d+)?(?:\D|$)"),
    re.compile(r"(?i)(?:^|[\s._\-\[\(])(?:серия|эпизод)\s*№?\s*0*(\d{1,4})(?:\D|$)"),
    re.compile(r"(?i)(?:^|[\s._\-\[\(])\d{1,2}x0*(\d{1,4})(?:\D|$)"),
    re.compile(r"(?i)(?:^|[\s._\-\[\(])0*(\d{1,4})(?:v\d+)?(?:[\s._\-\]\)]|$)"),
)

TECHNICAL_NUMBER_PATTERNS = (
    re.compile(r"(?i)\b(?:[1-9]\d{2,3})\s*[xх×]\s*(?:[1-9]\d{2,3})\b"),
    re.compile(r"(?i)\b(?:360|480|540|576|720|1080|1440|2160|4320)p\b"),
    re.compile(r"(?i)\b[хx](?:264|265|266)\b"),
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

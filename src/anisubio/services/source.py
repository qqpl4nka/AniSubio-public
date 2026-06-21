import re
from dataclasses import dataclass
from pathlib import PurePosixPath
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

from anisubio.config import Settings


ARCHIVE_SUFFIXES = {".zip", ".rar", ".7z"}


class SourceError(RuntimeError):
    pass


@dataclass(frozen=True)
class FansubsDownload:
    subtitle_id: int
    page_url: str
    download_url: str
    title: str


class FansubsSource:
    """Минимальный адаптер fansubs.ru.

    Он не привязан к хрупким CSS-классам: получает карточку аниме, находит
    POST-формы с полем ``srt`` и требует ручного выбора, если архивов несколько.
    """

    def __init__(self, settings: Settings):
        self.settings = settings

    def validate_url(self, url: str) -> None:
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"}:
            raise SourceError("Разрешены только HTTP/HTTPS URL")
        host = (parsed.hostname or "").lower()
        if host not in self.settings.allowed_source_hosts:
            raise SourceError(f"Источник {host!r} не разрешён")

    async def discover_download(
        self, page_url: str, preferred_subtitle_id: int | None = None
    ) -> FansubsDownload:
        self.validate_url(page_url)
        headers = {"User-Agent": self.settings.http_user_agent}
        async with httpx.AsyncClient(
            headers=headers,
            timeout=self.settings.request_timeout_seconds,
            follow_redirects=True,
        ) as client:
            response = await client.get(page_url)
            response.raise_for_status()

        soup = BeautifulSoup(response.text, "html.parser")
        candidates: list[int] = []
        for link in soup.select("a[href]"):
            candidate = urljoin(str(response.url), link.get("href", ""))
            parsed = urlparse(candidate)
            if PurePosixPath(parsed.path.lower()).suffix in ARCHIVE_SUFFIXES:
                self.validate_url(candidate)
                raise SourceError(
                    "Обнаружена прямая ссылка на архив; используйте archive_url="
                    + candidate
                )

        for form in soup.select("form"):
            method = (form.get("method") or "get").lower()
            field = form.select_one('input[name="srt"][value]')
            if method != "post" or field is None:
                continue
            value = field.get("value", "")
            if re.fullmatch(r"\d+", value) and int(value) not in candidates:
                candidates.append(int(value))

        if preferred_subtitle_id is not None:
            if preferred_subtitle_id not in candidates:
                raise SourceError("Указанный srt ID не найден на странице")
            selected = preferred_subtitle_id
        elif not candidates:
            raise SourceError("На странице не найдено форм загрузки субтитров")
        elif len(candidates) > 1:
            raise SourceError(
                "На странице найдено несколько архивов; передайте "
                "fansubs_subtitle_id: "
                + ", ".join(map(str, candidates[:20]))
            )
        else:
            selected = candidates[0]

        return FansubsDownload(
            subtitle_id=selected,
            page_url=str(response.url),
            download_url=urljoin(str(response.url), "/base.php"),
            title=soup.title.get_text(" ", strip=True) if soup.title else "",
        )

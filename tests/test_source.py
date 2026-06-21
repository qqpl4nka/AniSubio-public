import httpx
import pytest

from anisubio.services.source import FansubsSource, SourceError


@pytest.mark.anyio
async def test_discovers_fansubs_post_form(settings, monkeypatch) -> None:
    html = """
    <html><head><title>Example Anime</title></head><body>
      <form method="post" action="base.php">
        <input type="hidden" name="srt" value="13778">
        <input name="image" type="image" alt="Скачать - ASS">
      </form>
    </body></html>
    """

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            text=html,
            headers={"content-type": "text/html; charset=windows-1251"},
            request=request,
        )

    async_client = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: async_client(
            transport=httpx.MockTransport(handler), **kwargs
        ),
    )

    result = await FansubsSource(settings).discover_download(
        "http://fansubs.ru/base.php?id=7432"
    )

    assert result.subtitle_id == 13778
    assert result.download_url == "http://fansubs.ru/base.php"
    assert result.title == "Example Anime"


@pytest.mark.anyio
async def test_requires_selection_for_multiple_forms(settings, monkeypatch) -> None:
    html = """
      <form method="post"><input name="srt" value="1"></form>
      <form method="post"><input name="srt" value="2"></form>
    """

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=html, request=request)

    async_client = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: async_client(
            transport=httpx.MockTransport(handler), **kwargs
        ),
    )

    with pytest.raises(SourceError, match="несколько архивов"):
        await FansubsSource(settings).discover_download(
            "http://fansubs.ru/base.php?id=7432"
        )

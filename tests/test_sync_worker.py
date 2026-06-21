import pytest

from anisubio.sync_worker import (
    AnimeMetadata,
    FansubsIndexer,
    MetadataResolver,
    PoliteHttpClient,
    SyncError,
    normalize_title,
)


class FakeClient:
    def __init__(self, *, json_responses=None, payload=b"", headers=None):
        self.json_responses = list(json_responses or [])
        self.payload = payload
        self.headers = headers or {}
        self.requests = []

    async def get_json(self, url, headers=None):
        self.requests.append(("GET_JSON", url, headers))
        return self.json_responses.pop(0)

    async def request(self, method, url, **kwargs):
        self.requests.append((method, url, kwargs))
        return self.payload, self.headers


@pytest.mark.anyio
async def test_resolves_kitsu_to_shikimori_title() -> None:
    client = FakeClient(
        json_responses=[
            {
                "data": [
                    {
                        "attributes": {
                            "externalSite": "myanimelist/anime",
                            "externalId": "20",
                        }
                    }
                ]
            },
            {
                "name": "Naruto",
                "russian": "Наруто",
                "english": ["Naruto"],
                "japanese": ["ナルト"],
                "synonyms": ["NARUTO"],
            },
        ]
    )

    metadata = await MetadataResolver(client).resolve(11)

    assert metadata.kitsu_id == 11
    assert metadata.mal_id == 20
    assert metadata.russian_title == "Наруто"
    assert "Naruto" in metadata.aliases


@pytest.mark.anyio
async def test_requires_exact_fansubs_title_match() -> None:
    html = """
    <li><a href="base.php?id=5726">Boruto: Naruto Next Generations
      <small>(ТВ)</small></a></li>
    <li><a href="base.php?id=274">Naruto <small>(ТВ)</small></a></li>
    """
    client = FakeClient(payload=html.encode("cp1251"))
    metadata = AnimeMetadata(11, 20, "Наруто", ("Наруто", "Naruto"))

    result = await FansubsIndexer(client).find_exact_match(metadata)

    assert result.title == "Naruto"
    assert result.page_url == "http://fansubs.ru/base.php?id=274"
    body = client.requests[0][2]["data"]
    assert body == b"query=%CD%E0%F0%F3%F2%EE"


@pytest.mark.anyio
async def test_rejects_fuzzy_only_match() -> None:
    html = '<a href="base.php?id=5726">Boruto: Naruto Next Generations</a>'
    client = FakeClient(payload=html.encode("cp1251"))
    metadata = AnimeMetadata(11, 20, "Наруто", ("Наруто", "Naruto"))

    with pytest.raises(SyncError, match="точное совпадение"):
        await FansubsIndexer(client).find_exact_match(metadata)


@pytest.mark.anyio
async def test_searches_alternative_alias_after_russian_title_miss() -> None:
    client = FakeClient()
    responses = iter(
        [
            "<html></html>".encode("cp1251"),
            '<a href="base.php?id=274">Naruto <small>(TV)</small></a>'.encode(
                "cp1251"
            ),
        ]
    )

    async def request(method, url, **kwargs):
        client.requests.append((method, url, kwargs))
        return next(responses), {}

    client.request = request
    metadata = AnimeMetadata(11, 20, "Наруто", ("Наруто", "Naruto"))

    result = await FansubsIndexer(client).find_exact_match(metadata)

    assert result.page_url == "http://fansubs.ru/base.php?id=274"
    assert len(client.requests) == 2


@pytest.mark.anyio
async def test_discovers_all_archives_on_card() -> None:
    html = """
      <form method="post"><input name="srt" value="2579"></form>
      <form method="POST"><input name="srt" value="1293"></form>
      <form method="post"><input name="srt" value="2579"></form>
    """
    client = FakeClient(payload=html.encode("cp1251"))

    archives = await FansubsIndexer(client).discover_archives(
        "http://fansubs.ru/base.php?id=274"
    )

    assert [item.subtitle_id for item in archives] == [1293, 2579]
    assert all(item.download_url == "http://fansubs.ru/base.php" for item in archives)


def test_title_normalization() -> None:
    assert normalize_title("Наруто: Ураганные хроники") == normalize_title(
        "НАРУТО — Ураганные хроники!"
    )


def test_polite_http_client_initializes_process_lock(tmp_path) -> None:
    client = PoliteHttpClient(
        5,
        5,
        30,
        rate_limit_file=tmp_path / "http-rate-limit",
    )

    assert client._lock is not None
    assert client._last_request_at is None

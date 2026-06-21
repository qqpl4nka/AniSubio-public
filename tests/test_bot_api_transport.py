from pathlib import Path

import pytest
from aiohttp import web

from anisubio.storage.telegram import BotApiTransport


@pytest.mark.anyio
async def test_bot_api_upload_get_file_and_download(
    tmp_path: Path,
    free_tcp_port: int,
) -> None:
    uploaded = b""

    async def send_document(request: web.Request) -> web.Response:
        nonlocal uploaded
        reader = await request.multipart()
        fields = {}
        async for part in reader:
            if part.name == "document":
                uploaded = await part.read()
            else:
                fields[part.name] = await part.text()
        return web.json_response(
            {
                "ok": True,
                "result": {
                    "message_id": 42,
                    "chat": {"id": int(fields["chat_id"])},
                    "document": {
                        "file_id": "file-123",
                        "file_unique_id": "unique-123",
                        "file_name": "episode.ass",
                        "mime_type": "text/x-ssa",
                        "file_size": len(uploaded),
                    },
                },
            }
        )

    async def get_file(request: web.Request) -> web.Response:
        data = await request.post()
        assert data["file_id"] == "file-123"
        return web.json_response(
            {"ok": True, "result": {"file_path": "documents/episode.ass"}}
        )

    async def download(_request: web.Request) -> web.Response:
        return web.Response(body=uploaded)

    app = web.Application()
    app.router.add_post("/bottest/sendDocument", send_document)
    app.router.add_post("/bottest/getFile", get_file)
    app.router.add_get("/file/bottest/documents/episode.ass", download)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", free_tcp_port)
    await site.start()
    try:
        transport = BotApiTransport(
            "test",
            proxy_url="",
            api_base=f"http://127.0.0.1:{free_tcp_port}",
        )
        source = tmp_path / "episode.ass"
        source.write_bytes(b"[Script Info]\n")
        stored = await transport.upload_document(-100123, source, "sha256=abc")
        destination = tmp_path / "downloaded.ass"
        await transport.download_to(stored.object_id, destination)

        assert stored.file_id == "file-123"
        assert stored.file_unique_id == "unique-123"
        assert stored.message_id == 42
        assert destination.read_bytes() == source.read_bytes()
    finally:
        await runner.cleanup()

"""The handshake gate and the hosted page of ``tau serve`` (docs/TAU-SERVE.md §6.2)."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from websockets.asyncio.client import connect
from websockets.asyncio.server import serve as ws_serve
from websockets.exceptions import InvalidStatus

from tau_coding_agent.serve.http import build_process_request, origin_allowed


async def _echo(ws) -> None:
    async for message in ws:
        await ws.send(message)


async def _served(serve_config: dict, logged: list[str]):
    hook = build_process_request(serve_config, logged.append)
    server = await ws_serve(_echo, "127.0.0.1", 0, process_request=hook)
    return server, server.sockets[0].getsockname()[1]


async def _http_get(port: int, path: str) -> tuple[int, bytes]:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(f"GET {path} HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n\r\n".encode())
    await writer.drain()
    raw = await reader.read()
    writer.close()
    head, _, body = raw.partition(b"\r\n\r\n")
    return int(head.split()[1]), body


def test_origin_rules():
    none = frozenset[str]()
    assert origin_allowed(None, "127.0.0.1:8256", none), "non-browser clients send no Origin"
    assert origin_allowed("http://127.0.0.1:8256", "127.0.0.1:8256", none)
    assert origin_allowed("http://LOCALHOST:9000", "localhost:9000", none), (
        "ssh -L remaps keep working"
    )
    assert not origin_allowed("https://evil.example", "127.0.0.1:8256", none)
    assert not origin_allowed("null", "127.0.0.1:8256", none)
    assert origin_allowed(
        "http://localhost:5173", "127.0.0.1:8256", frozenset({"http://localhost:5173"})
    )


async def test_a_foreign_origin_is_refused_and_a_client_without_one_is_not():
    logged: list[str] = []
    server, port = await _served({}, logged)
    try:
        async with connect(f"ws://127.0.0.1:{port}/") as ws:
            await ws.send("hi")
            assert await ws.recv() == "hi"
        with pytest.raises(InvalidStatus) as refused:
            await connect(f"ws://127.0.0.1:{port}/", origin="https://evil.example")
        assert refused.value.response.status_code == 403
        assert "https://evil.example" in logged[0]
        async with connect(f"ws://127.0.0.1:{port}/", origin=f"http://127.0.0.1:{port}"):
            pass
    finally:
        server.close()
        await server.wait_closed()


async def test_the_web_root_is_served_and_nothing_outside_it(tmp_path: Path):
    root = tmp_path / "web"
    (root / "assets").mkdir(parents=True)
    (root / "index.html").write_text("<p>tau</p>")
    (root / "assets" / "app.js").write_text("1")
    (tmp_path / "secret.txt").write_text("no")
    server, port = await _served({"web_root": str(root)}, [])
    try:
        assert await _http_get(port, "/") == (200, b"<p>tau</p>")
        assert await _http_get(port, "/assets/app.js") == (200, b"1")
        assert (await _http_get(port, "/../secret.txt"))[0] == 404
        assert (await _http_get(port, "/%2e%2e/secret.txt"))[0] == 404
    finally:
        server.close()
        await server.wait_closed()


async def test_without_a_web_root_plain_http_says_how_to_get_one():
    server, port = await _served({}, [])
    try:
        status, body = await _http_get(port, "/")
        assert status == 404 and b"serve.web_root" in body
    finally:
        server.close()
        await server.wait_closed()


def test_a_web_root_that_is_not_a_directory_fails_at_start(tmp_path: Path):
    with pytest.raises(ValueError, match="not a directory"):
        build_process_request({"web_root": str(tmp_path / "missing")}, print)

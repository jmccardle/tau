"""Recorded llama.cpp responses (fixtures/) served through `httpx.MockTransport`.

The fixtures are raw bytes from a real server (Qwen3, llama.cpp b1637), recorded with
`stream: true`; nothing here edits them.
"""

import asyncio
import json
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from pathlib import Path
from typing import cast

import httpx

from tau_next import (
    Config,
    Driver,
    ModelConfig,
    Node,
    Part,
    SampleError,
    Session,
    Stop,
    SystemClock,
)
from tau_next.providers.openai import OpenAIProvider

FIXTURES = Path(__file__).parent / "fixtures"
BASE_URL = "http://llama.test:8100/v1"
MODEL = "/fast/Qwen3.8-27B-absolute-heresy-Q4_K_M.gguf"
CONFIG = ModelConfig(MODEL, BASE_URL).to_config()


class Server:
    """A transport that answers every request with `respond` and keeps each request seen."""

    def __init__(self, response: httpx.Response | Exception) -> None:
        self._response = response
        self.requests: list[httpx.Request] = []

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if isinstance(self._response, Exception):
            raise self._response
        return self._response

    @property
    def bodies(self) -> list[dict[str, object]]:
        return [json.loads(r.content) for r in self.requests]


def sse(name: str) -> Server:
    """A server that streams `fixtures/<name>.sse`."""
    content = (FIXTURES / f"{name}.sse").read_bytes()
    return Server(
        httpx.Response(200, headers={"content-type": "text/event-stream"}, content=content)
    )


def sse_text(text: str) -> Server:
    """A server that streams `text`, which a test wrote by hand (a malformed or cut-off stream)."""
    return Server(httpx.Response(200, headers={"content-type": "text/event-stream"}, text=text))


def recorded_error() -> Server:
    """A server that answers with `fixtures/http_error.http`: status line, headers, body."""
    head, _, body = (FIXTURES / "http_error.http").read_bytes().partition(b"\r\n\r\n")
    status = int(head.split(b" ", 2)[1])
    return Server(httpx.Response(status, content=body))


class GatedStream(httpx.AsyncByteStream):
    """A body that sends `head`, waits until `gate` is set, then sends `tail`."""

    def __init__(self, head: bytes, gate: asyncio.Event, tail: bytes) -> None:
        self._head = head
        self._gate = gate
        self._tail = tail

    async def __aiter__(self) -> AsyncIterator[bytes]:
        yield self._head
        await self._gate.wait()
        yield self._tail


class CutStream(httpx.AsyncByteStream):
    """A body that sends `head`, then breaks the connection."""

    def __init__(self, head: bytes) -> None:
        self._head = head

    async def __aiter__(self) -> AsyncIterator[bytes]:
        yield self._head
        raise httpx.ReadError("connection reset by peer")


def chunk(delta: dict[str, object], finish: str | None = None) -> str:
    """One SSE event in llama.cpp's shape."""
    choice = {"index": 0, "delta": delta, "finish_reason": finish}
    return f"data: {json.dumps({'choices': [choice]})}\n\n"


async def collect(
    provider: OpenAIProvider, walk: Sequence[Node], config: Config = CONFIG
) -> tuple[list[Part | Stop], SampleError | None]:
    """Everything `sample` yielded, and the `SampleError` it ended with, if any."""
    items: list[Part | Stop] = []
    try:
        async for item in provider.sample(walk, config):
            items.append(item)
    except SampleError as e:
        return items, e
    return items, None


def fresh_walk() -> list[Node]:
    """The walk of a new in-memory session: `Config`, `System`."""
    session = Session.create(CONFIG, "sys", 0)
    walk = session.walk(session.at(session.cursors()[0]))
    session.close()
    return walk


@asynccontextmanager
async def running(
    directory: Path, provider: OpenAIProvider, config: Config = CONFIG
) -> AsyncIterator[tuple[Session, Driver, int]]:
    """A session on disk in `directory` and a started driver over `provider`."""
    session = Session.create(config, "sys", SystemClock().now(), directory)
    driver = Driver(session, provider)
    driver.start()
    try:
        yield session, driver, session.cursors()[0]
    finally:
        await driver.stop()
        session.close()


def sent(body: dict[str, object]) -> list[dict[str, object]]:
    """The `messages` of a request body."""
    return cast("list[dict[str, object]]", body["messages"])

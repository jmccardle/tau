"""Recorded llama.cpp responses (fixtures/) served through `httpx.MockTransport`.

The fixtures are raw bytes from a real server (Qwen3, llama.cpp b1637), recorded with
`stream: true`; nothing here edits them. The exception is `plain_usage.sse`: `plain.sse` plus
the trailing chunk llama.cpp sends when asked for usage (empty `choices`, `usage`, `timings`),
built by hand from the recorded chunk's own fields, not re-recorded.
"""

import asyncio
import json
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from pathlib import Path
from typing import cast

import httpx

from tau_next import (
    Driver,
    Finish,
    ModelCatalog,
    ModelConfig,
    Node,
    Part,
    SampleError,
    Session,
    SystemClock,
    Tool,
    model_config,
    read_models,
)
from tau_next.providers.openai import OpenAIProvider

FIXTURES = Path(__file__).parent / "fixtures"
BASE_URL = "http://llama.test:8100/v1"
MODEL = "/fast/Qwen3.8-27B-absolute-heresy-Q4_K_M.gguf"
NAME = "llama"
CONFIG = model_config(NAME)
RESOLVED = ModelConfig(MODEL, BASE_URL)


def catalog(
    api_key_env: str | None = None, model: str = MODEL, base_url: str = BASE_URL
) -> ModelCatalog:
    """A catalog whose one model `NAME` is `model` at `base_url` (`RESOLVED` by default),
    reading its key from `api_key_env`."""
    settings = {"model": model, "base_url": base_url}
    if api_key_env is not None:
        settings["api_key_env"] = api_key_env
    return read_models(cli={"models": {NAME: settings}})


class Server:
    """A transport that answers requests with `response` and keeps each request seen. Given
    a list, it answers the first request with the first, and so on, then repeats the last."""

    def __init__(self, response: httpx.Response | Exception | list[httpx.Response]) -> None:
        self._responses = response if isinstance(response, list) else [response]
        self.requests: list[httpx.Request] = []

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        response = self._responses[min(len(self.requests), len(self._responses)) - 1]
        if isinstance(response, Exception):
            raise response
        return response

    @property
    def bodies(self) -> list[dict[str, object]]:
        return [json.loads(r.content) for r in self.requests]


def sse(name: str) -> Server:
    """A server that streams `fixtures/<name>.sse`."""
    content = (FIXTURES / f"{name}.sse").read_bytes()
    return Server(
        httpx.Response(200, headers={"content-type": "text/event-stream"}, content=content)
    )


def sse_sequence(*names: str) -> Server:
    """A server that streams `fixtures/<name>.sse` for each request in turn, the last repeated."""
    headers = {"content-type": "text/event-stream"}
    return Server(
        [
            httpx.Response(200, headers=headers, content=(FIXTURES / f"{name}.sse").read_bytes())
            for name in names
        ]
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
    provider: OpenAIProvider,
    walk: Sequence[Node],
    model: ModelConfig = RESOLVED,
    tools: Sequence[Tool] = (),
) -> tuple[list[Part | Finish], SampleError | None]:
    """Everything `sample` yielded, and the `SampleError` it ended with, if any."""
    items: list[Part | Finish] = []
    try:
        async for item in provider.sample(walk, model, tools):
            items.append(item)
    except SampleError as e:
        return items, e
    return items, None


def fresh_walk() -> list[Node]:
    """The walk of a new in-memory session: `Config`, `System`."""
    session = Session.create(CONFIG, "sys", 0)
    walk = session.tree.walk(session.cursor(session.cursors()[0]).at)
    session.close()
    return walk


@asynccontextmanager
async def running(
    directory: Path, provider: OpenAIProvider, models: ModelCatalog | None = None
) -> AsyncIterator[tuple[Session, Driver, int]]:
    """A session on disk in `directory` naming `NAME`, and a started driver over `provider`
    and `models` (`catalog()` by default)."""
    session = Session.create(CONFIG, "sys", SystemClock().now(), directory)
    driver = Driver(session, {"openai": provider}, models or catalog())
    driver.start()
    try:
        yield session, driver, session.cursors()[0]
    finally:
        await driver.stop()
        session.close()


def sent(body: dict[str, object]) -> list[dict[str, object]]:
    """The `messages` of a request body."""
    return cast("list[dict[str, object]]", body["messages"])

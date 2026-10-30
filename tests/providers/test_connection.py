"""What the three hosted providers share over HTTP (`_wire.Connection`), tested once and run
through each: one long-lived client, the loop it belongs to, and `ProviderClosed`."""

import asyncio
import threading
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, replace

import httpx
import pytest
from anthropic_streams import ENVIRON as ANTHROPIC_ENVIRON
from anthropic_streams import KEY_ENV as ANTHROPIC_KEY_ENV
from anthropic_streams import RESOLVED as ANTHROPIC_MODEL
from anthropic_streams import delta as anthropic_delta
from anthropic_streams import event, message, opened, start, text
from anthropic_streams import finish as anthropic_finish
from fakes import FakeTool
from gemini_support import ENVIRON as GEMINI_ENVIRON
from gemini_support import gemini_chunk, gemini_walk, model, walk_of
from harness import scenario
from transcripts import MODEL as OPENAI_MODEL
from transcripts import chunk

from tau_next import (
    Author,
    Body,
    Delta,
    Finish,
    ModelConfig,
    Outcome,
    Part,
    Provider,
    ProviderClosed,
    SampleError,
    Stop,
    TextDelta,
)
from tau_next.providers.anthropic import AnthropicProvider
from tau_next.providers.gemini import GeminiProvider
from tau_next.providers.openai import OpenAIProvider

SSE = {"content-type": "text/event-stream"}


@dataclass(frozen=True)
class Dialect:
    """One provider: how to build it, its model at a base URL, and a reply that says `hi`
    (`stalled` is that reply up to its first delta)."""

    make: Callable[[httpx.AsyncBaseTransport | None], Provider]
    model: Callable[[str], ModelConfig]  # takes the base URL of a `KeepAliveServer`
    reply: str
    stalled: str


DIALECTS = {
    "openai": Dialect(
        lambda transport: OpenAIProvider(transport, {}),
        lambda url: ModelConfig(OPENAI_MODEL, url),
        chunk({"content": "hi"}) + chunk({}, "stop") + "data: [DONE]\n\n",
        chunk({"content": "hi"}),
    ),
    "anthropic": Dialect(
        lambda transport: AnthropicProvider(transport, ANTHROPIC_ENVIRON),
        lambda url: replace(ANTHROPIC_MODEL, base_url=url.removesuffix("/v1")),
        message(text(0, ["hi"])),
        start()
        + event("ping")
        + opened(0, {"type": "text", "text": ""})
        + anthropic_delta(0, type="text_delta", text="hi"),
    ),
    "gemini": Dialect(
        lambda transport: GeminiProvider(transport, GEMINI_ENVIRON),
        lambda url: replace(model(), base_url=url),
        gemini_chunk([{"text": "hi"}]) + gemini_chunk([], "STOP"),
        gemini_chunk([{"text": "hi"}]),
    ),
}
EACH = pytest.mark.parametrize("dialect", list(DIALECTS.values()), ids=list(DIALECTS))


class CountingTransport(httpx.AsyncBaseTransport):
    """A network stand-in that counts the requests it serves and the times it is closed: a
    client that is closed per sample closes its transport per sample."""

    def __init__(self, text: str) -> None:
        self._text = text
        self.requests = 0
        self.closes = 0

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.requests += 1
        return httpx.Response(200, headers=SSE, text=self._text)

    async def aclose(self) -> None:
        self.closes += 1


class KeepAliveServer:
    """A real HTTP/1.1 server on 127.0.0.1, in its own thread and event loop (so it outlives
    the loops a test runs samples on). It answers every request with `body` as an SSE
    response of known length, on a connection it keeps open, and counts the connections.
    With `stall`, it instead sends that much of `body`, promises more, and goes quiet."""

    def __init__(self, body: str, stall: int | None = None) -> None:
        self._body = body.encode()
        self._stall = stall
        self.connections = 0
        self._loop = asyncio.new_event_loop()
        self._server: asyncio.Server | None = None

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.connections += 1
        try:
            while True:
                head = await reader.readuntil(b"\r\n\r\n")
                length = 0
                for line in head.split(b"\r\n"):
                    if line.lower().startswith(b"content-length:"):
                        length = int(line.split(b":")[1])
                await reader.readexactly(length)
                size = len(self._body) + (0 if self._stall is None else 10)
                writer.write(
                    b"HTTP/1.1 200 OK\r\ncontent-type: text/event-stream\r\n"
                    + f"content-length: {size}\r\n\r\n".encode()
                )
                writer.write(self._body if self._stall is None else self._body[: self._stall])
                await writer.drain()
                if self._stall is not None:
                    await asyncio.sleep(60)  # the promised rest never comes
        except (EOFError, ConnectionError, asyncio.CancelledError):
            pass
        finally:
            writer.close()

    async def _stop(self) -> None:
        """Close the listener and end every connection handler while the loop still runs, so
        none is left pending when it stops."""
        assert self._server is not None
        self._server.close()
        handlers = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
        for task in handlers:
            task.cancel()
        await asyncio.gather(*handlers, return_exceptions=True)

    @contextmanager
    def running(self) -> Iterator[str]:
        """The base URL of the server, while it runs."""
        thread = threading.Thread(target=self._loop.run_forever, daemon=True)
        thread.start()

        async def start_server() -> asyncio.Server:
            return await asyncio.start_server(self._serve, "127.0.0.1", 0)

        self._server = asyncio.run_coroutine_threadsafe(start_server(), self._loop).result()
        port = self._server.sockets[0].getsockname()[1]
        try:
            yield f"http://127.0.0.1:{port}/v1"
        finally:
            asyncio.run_coroutine_threadsafe(self._stop(), self._loop).result()
            self._loop.call_soon_threadsafe(self._loop.stop)
            thread.join()
            self._loop.close()


async def sample_all(
    provider: Provider, config: ModelConfig
) -> tuple[list[Part | Finish | Delta], SampleError | None]:
    """Everything `sample` yielded, and the `SampleError` it ended with, if any."""
    items: list[Part | Finish | Delta] = []
    try:
        async for item in provider.sample(gemini_walk(), config, []):
            items.append(item)
    except SampleError as e:
        return items, e
    return items, None


@EACH
@scenario
async def test_many_samples_share_one_client_until_aclose(dialect: Dialect) -> None:
    transport = CountingTransport(dialect.reply)
    provider = dialect.make(transport)
    config = dialect.model("http://unused.test/v1")
    for _ in range(4):
        _, error = await sample_all(provider, config)
        assert error is None
    assert (transport.requests, transport.closes) == (4, 0)
    await provider.aclose()
    assert transport.closes == 1
    await provider.aclose()  # closing twice is not a failure
    assert transport.closes == 1


@EACH
@scenario
async def test_a_sample_after_aclose_fails_as_closed_not_as_a_transient_error(
    dialect: Dialect,
) -> None:
    provider = dialect.make(CountingTransport(dialect.reply))
    await provider.aclose()
    items, error = await sample_all(provider, dialect.model("http://unused.test/v1"))
    assert items == []
    assert isinstance(error, ProviderClosed)
    assert (error.status, error.retry_after) == (None, None)


@EACH
@scenario
async def test_two_sequential_samples_use_one_connection(dialect: Dialect) -> None:
    server = KeepAliveServer(dialect.reply)
    with server.running() as url:
        provider = dialect.make(None)
        for _ in range(3):
            items, error = await sample_all(provider, dialect.model(url))
            assert error is None
            assert [i for i in items if isinstance(i, Part)] == [Part.Text("hi")]
            assert isinstance(items[-1], Finish)
        await provider.aclose()
    assert server.connections == 1


@EACH
def test_a_program_that_runs_one_event_loop_per_command_keeps_working(dialect: Dialect) -> None:
    server = KeepAliveServer(dialect.reply)
    with server.running() as url:
        provider = dialect.make(None)
        for _ in range(2):
            items, error = asyncio.run(sample_all(provider, dialect.model(url)))
            assert error is None
            assert Part.Text("hi") in items
        asyncio.run(provider.aclose())


@EACH
@scenario
async def test_aclose_cuts_a_sample_in_flight_as_closed(dialect: Dialect) -> None:
    server = KeepAliveServer(dialect.reply, stall=len(dialect.stalled))
    with server.running() as url:
        provider = dialect.make(None)
        stream = provider.sample(gemini_walk(), dialect.model(url), [])
        assert await anext(stream) == TextDelta("hi")
        rest = asyncio.ensure_future(collect_rest(stream))
        await asyncio.sleep(0.05)
        await provider.aclose()
        parts, error = await asyncio.wait_for(rest, 5)
    assert parts == [Part.Text("hi")], "the text that arrived is still yielded"
    assert isinstance(error, ProviderClosed)


async def collect_rest(stream: AsyncIterator[object]) -> tuple[list[object], SampleError | None]:
    parts: list[object] = []
    try:
        async for item in stream:
            parts.append(item)
    except SampleError as e:
        return parts, e
    return parts, None


def sse_of(text: str) -> CountingTransport:
    return CountingTransport(text)


@EACH
@scenario
async def test_a_nan_or_infinity_in_a_stream_event_is_a_sample_error(dialect: Dialect) -> None:
    for constant in ("NaN", "Infinity", "-Infinity"):
        provider = dialect.make(sse_of(f'data: {{"usage": {{"x": {constant}}}}}\n\n'))
        items, error = await sample_all(provider, dialect.model("http://unused.test/v1"))
        assert items == []
        assert error is not None
        assert "malformed SSE data" in str(error)


@EACH
@scenario
async def test_a_lone_surrogate_in_a_stream_event_is_a_sample_error(dialect: Dialect) -> None:
    provider = dialect.make(sse_of('data: {"x": "\\ud800"}\n\n'))
    items, error = await sample_all(provider, dialect.model("http://unused.test/v1"))
    assert items == []
    assert error is not None
    assert "surrogate" in str(error)


@EACH
@scenario
async def test_a_paired_surrogate_escape_is_text_not_an_error(dialect: Dialect) -> None:
    provider = dialect.make(sse_of(dialect.reply.replace("hi", "\\ud83d\\ude00")))
    items, error = await sample_all(provider, dialect.model("http://unused.test/v1"))
    assert error is None
    assert Part.Text("\U0001f600") in items


@scenario
async def test_an_option_too_big_for_a_float_is_a_problem_not_a_crash() -> None:
    config = ModelConfig(OPENAI_MODEL, "http://unused.test/v1", options={"temperature": 10**400})
    assert any("temperature" in p for p in OpenAIProvider(None, {}).check(config))


@EACH
@scenario
async def test_a_retry_after_that_is_not_finite_is_no_wait(dialect: Dialect) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, headers={"retry-after": "9" * 400}, text="slow down")

    provider = dialect.make(httpx.MockTransport(handler))
    _, error = await sample_all(provider, dialect.model("http://unused.test/v1"))
    assert error is not None
    assert (error.status, error.retry_after) == (429, None)


@EACH
@scenario
async def test_a_transport_error_is_the_cause_of_its_sample_error(dialect: Dialect) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    provider = dialect.make(httpx.MockTransport(handler))
    _, error = await sample_all(provider, dialect.model("http://unused.test/v1"))
    assert error is not None
    assert isinstance(error.__cause__, httpx.TransportError)


SECRET_KEYS = {
    "newline": "sk-secret-0123\nrest",
    "smart quote": "sk-secret-0123\u201cquoted\u201d",
}


@pytest.mark.parametrize("key", list(SECRET_KEYS.values()), ids=list(SECRET_KEYS))
@scenario
async def test_a_key_http_cannot_carry_fails_the_sample_without_quoting_it(key: str) -> None:
    dialect = DIALECTS["anthropic"]
    server = KeepAliveServer(dialect.reply)
    with server.running() as url:
        provider = AnthropicProvider(None, {ANTHROPIC_KEY_ENV: key})
        items, error = await sample_all(provider, dialect.model(url))
        await provider.aclose()
    assert items == []
    assert error is not None
    assert ANTHROPIC_KEY_ENV in str(error)  # refused by `ModelConfig.api_key`, before any header
    assert "sk-secret" not in str(error)
    assert "sk-secret" not in repr(error.__cause__)


@scenario
async def test_a_non_finite_number_in_the_request_is_a_sample_error() -> None:
    provider = OpenAIProvider(sse_of(DIALECTS["openai"].reply), {})
    config = ModelConfig(
        OPENAI_MODEL, "http://unused.test/v1", options={"extra_body": {"x": float("nan")}}
    )
    items, error = await sample_all(provider, config)
    assert items == []
    assert error is not None
    assert "can't carry" in str(error)


@scenario
async def test_content_after_the_stop_is_dropped_and_the_reply_still_finishes() -> None:
    body = start() + text(0, ["hi"]) + anthropic_finish("end_turn") + text(1, ["late"])
    provider = AnthropicProvider(sse_of(body), ANTHROPIC_ENVIRON)
    items, error = await sample_all(provider, ANTHROPIC_MODEL)
    assert error is None
    assert [i for i in items if isinstance(i, Part)] == [Part.Text("hi")]
    assert isinstance(items[-1], Finish)
    assert items[-1].stop == Stop.End


def sent_kinds(args_raw: str) -> list[str]:
    """The block types Anthropic sends for a walk whose one call has `args_raw`."""
    walk = walk_of(
        Body.User("go"),
        Body.Assistant([Part.Call("a", "ta", args_raw)], Stop.Tools),
        Body.Results([("a", Outcome.Ok("done"), Author.Core())]),
    )
    request = AnthropicProvider(None, ANTHROPIC_ENVIRON).request(
        walk, ANTHROPIC_MODEL, [FakeTool("ta", "A")]
    )
    messages: list[dict[str, list[dict[str, str]]]] = request[2]["messages"]  # type: ignore[assignment]
    return [block["type"] for message in messages for block in message["content"]]


def test_arguments_with_a_nan_or_a_lone_surrogate_send_the_batch_as_text() -> None:
    assert "tool_use" in sent_kinds('{"x": 1}')
    for bad in ('{"x": NaN}', '{"x": Infinity}', '{"x": "\\ud800"}'):
        assert "tool_use" not in sent_kinds(bad), bad

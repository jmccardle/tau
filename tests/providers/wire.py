"""What every provider test shares: servers and transports, the streams of the three hosted
APIs, walks, a driver over any provider, and one `Dialect` per provider so a behaviour that
does not depend on the provider is written once and run through each.

The recorded fixtures (`fixtures/`) are raw bytes from real servers; nothing here edits them.
The exception is `plain_usage.sse`: `plain.sse` plus the trailing chunk llama.cpp sends when
asked for usage, built by hand from the recorded chunk's own fields. The Anthropic streams are
written by hand in the event shapes of the streaming docs. Nothing here touches the network."""

import asyncio
import json
import threading
from collections.abc import AsyncIterator, Callable, Iterator, Mapping, Sequence
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import cast

import httpx
import pytest

from tau import (
    Author,
    Body,
    Delta,
    Driver,
    Finish,
    ModelCatalog,
    ModelConfig,
    Node,
    Part,
    Provider,
    SampleError,
    Session,
    SystemClock,
    Tool,
    model_config,
    read_models,
)
from tau.providers.anthropic import AnthropicProvider
from tau.providers.gemini import GeminiProvider
from tau.providers.openai import OpenAIProvider

FIXTURES = Path(__file__).parent / "fixtures"
SSE = {"content-type": "text/event-stream"}
KEY_ENV = "TAU_TEST_KEY"
SECRET = "sk-test-0123456789"
ENVIRON = {KEY_ENV: SECRET}
ALIAS = "m"  # the catalog name every dialect's model has
UNUSED = "http://unused.test"  # a root nothing listens at: the transport answers instead


# Servers and transports


class Server:
    """A transport that answers requests with `response` and keeps each request seen. Given
    a list, it answers the first request with the first, and so on, then repeats the last. An
    exception is raised from the transport instead of answering."""

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
    return sse_sequence(name)


def sse_sequence(*names: str) -> Server:
    """A server that streams `fixtures/<name>.sse` for each request in turn, the last repeated."""
    return sse_texts(*(recorded(name) for name in names))


def sse_text(text: str) -> Server:
    """A server that streams `text`, which a test wrote by hand."""
    return sse_texts(text)


def sse_texts(*texts: str) -> Server:
    """A server that streams each of `texts` for each request in turn, the last repeated."""
    return Server([httpx.Response(200, headers=SSE, text=text) for text in texts])


def recorded(name: str) -> str:
    return (FIXTURES / f"{name}.sse").read_text()


def recorded_error() -> Server:
    """A server that answers with `fixtures/http_error.http`: status line, headers, body."""
    head, _, body = (FIXTURES / "http_error.http").read_bytes().partition(b"\r\n\r\n")
    return Server(httpx.Response(int(head.split(b" ", 2)[1]), content=body))


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


def cut(head: str) -> Server:
    """A server that streams `head` and then resets the connection."""
    return Server(httpx.Response(200, headers=SSE, stream=CutStream(head.encode())))


def gated(head: str) -> tuple[Server, asyncio.Event]:
    """A server that streams `head`, then holds the connection open until the event is set."""
    gate = asyncio.Event()
    return Server(
        httpx.Response(200, headers=SSE, stream=GatedStream(head.encode(), gate, b""))
    ), gate


class KeepAliveServer:
    """A real HTTP/1.1 server on 127.0.0.1, in its own thread and event loop (so it outlives
    the loops a test runs samples on). It answers every request with `body` as an SSE
    response of known length, on a connection it keeps open, and counts the connections and
    keeps each request's head. With `stall`, it instead sends that much of `body`, promises
    more, and goes quiet."""

    def __init__(self, body: str, stall: int | None = None) -> None:
        self._body = body.encode()
        self._stall = stall
        self.connections = 0
        self.heads: list[str] = []
        self._loop = asyncio.new_event_loop()
        self._server: asyncio.Server | None = None

    def header(self, name: str) -> str | None:
        """The value of header `name` on the last request, if it had one."""
        for line in self.heads[-1].split("\r\n"):
            key, _, value = line.partition(":")
            if key.lower() == name.lower():
                return value.strip()
        return None

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.connections += 1
        try:
            while True:
                head = await reader.readuntil(b"\r\n\r\n")
                self.heads.append(head.decode())
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
        """The root URL (`http://127.0.0.1:<port>`) of the server, while it runs."""
        thread = threading.Thread(target=self._loop.run_forever, daemon=True)
        thread.start()

        async def start_server() -> asyncio.Server:
            return await asyncio.start_server(self._serve, "127.0.0.1", 0)

        self._server = asyncio.run_coroutine_threadsafe(start_server(), self._loop).result()
        port = self._server.sockets[0].getsockname()[1]
        try:
            yield f"http://127.0.0.1:{port}"
        finally:
            asyncio.run_coroutine_threadsafe(self._stop(), self._loop).result()
            self._loop.call_soon_threadsafe(self._loop.stop)
            thread.join()
            self._loop.close()


# Streams: OpenAI-compatible (llama.cpp's shape)

DONE_LINE = "data: [DONE]\n\n"


def chunk(delta: dict[str, object], finish: str | None = None) -> str:
    """One SSE event in llama.cpp's shape."""
    choice = {"index": 0, "delta": delta, "finish_reason": finish}
    return f"data: {json.dumps({'choices': [choice]})}\n\n"


# Streams: Anthropic Messages API

SIGNATURE = "EqQBCgIYAhIM1gbcDa9GJwZA2b3hGgxBdjrkzLoky3dl1pkiMOYds/+=="
THINKING = "I need the GCD of 1071 and 462.\nnaïve   step: 1071 = 2 × 462 + 147"  # noqa: RUF001 - non-ASCII on purpose: replay is byte for byte


def event(kind: str, **fields: object) -> str:
    return f"event: {kind}\ndata: {json.dumps({'type': kind, **fields})}\n\n"


def start(usage: Mapping[str, object] | None = None) -> str:
    message = {
        "id": "msg_01XFDUDYJgAACzvnptvVoYEL",
        "type": "message",
        "role": "assistant",
        "content": [],
        "model": "claude-test-1",
        "stop_reason": None,
        "stop_sequence": None,
        "usage": usage or {"input_tokens": 25, "output_tokens": 1},
    }
    return event("message_start", message=message)


def opened(index: int, block: dict[str, object]) -> str:
    return event("content_block_start", index=index, content_block=block)


def delta(index: int, **fields: object) -> str:
    return event("content_block_delta", index=index, delta=fields)


def closed(index: int) -> str:
    return event("content_block_stop", index=index)


def thinking(index: int, chunks: Sequence[str], signature: str | None = SIGNATURE) -> str:
    out = opened(index, {"type": "thinking", "thinking": "", "signature": ""})
    out += "".join(delta(index, type="thinking_delta", thinking=c) for c in chunks)
    if signature is not None:
        out += delta(index, type="signature_delta", signature=signature)
    return out + closed(index)


def redacted(index: int, data: str) -> str:
    return opened(index, {"type": "redacted_thinking", "data": data}) + closed(index)


def text(index: int, chunks: Sequence[str]) -> str:
    out = opened(index, {"type": "text", "text": ""})
    out += "".join(delta(index, type="text_delta", text=c) for c in chunks)
    return out + closed(index)


def call(index: int, id_: str, name: str, chunks: Sequence[str]) -> str:
    out = opened(index, {"type": "tool_use", "id": id_, "name": name, "input": {}})
    out += "".join(delta(index, type="input_json_delta", partial_json=c) for c in chunks)
    return out + closed(index)


def finish(reason: str, usage: Mapping[str, object] | None = None) -> str:
    fields = {"stop_reason": reason, "stop_sequence": None}
    return event("message_delta", delta=fields, usage=usage or {"output_tokens": 15})


def message(
    *blocks: str, reason: str = "end_turn", usage: Mapping[str, object] | None = None
) -> str:
    """A whole response: `message_start`, a ping, the blocks, `message_delta`, `message_stop`."""
    ping = event("ping")
    return start() + ping + "".join(blocks) + finish(reason, usage) + event("message_stop")


# Streams: Gemini generateContent


def gemini_chunk(
    parts: Sequence[Mapping[str, object]],
    finish: str | None = None,
    usage: Mapping[str, object] | None = None,
) -> str:
    """One SSE event in the API's shape: a candidate with `parts`, maybe a `finishReason`."""
    candidate: dict[str, object] = {"content": {"parts": list(parts), "role": "model"}, "index": 0}
    if finish is not None:
        candidate["finishReason"] = finish
    chunk_: dict[str, object] = {"candidates": [candidate]}
    if usage is not None:
        chunk_["usageMetadata"] = usage
    return f"data: {json.dumps(chunk_)}\r\n\r\n"


# Dialects


@dataclass(frozen=True)
class Dialect:
    """One hosted provider, as far as a test that does not care which can tell: how to build
    it, its model at a root URL, its catalog, and streams that say what a test needs said.

    `reply(t)` is a whole reply saying `t`; `stalled` is that reply cut after its first delta;
    `thinking_head` is a stream still thinking ("let me ", "think") with nothing after it;
    `late` is a text block that arrives after a reply finished; `trouble` is an error event;
    `key_header` is where the key goes on the wire."""

    name: str
    model_id: str
    path: str  # what the provider's base URL adds to the root
    make: Callable[[httpx.AsyncBaseTransport | None, Mapping[str, str]], Provider]
    reply: Callable[[str], str]
    stalled: str
    thinking_head: str
    late: str
    trouble: str
    key_header: str  # the header the key travels in

    def model(self, root: str | None = UNUSED, **options: object) -> ModelConfig:
        """The model the driver resolves `ALIAS` to, at `root` (`None`: the provider's own URL)."""
        return ModelConfig(
            self.model_id,
            None if root is None else root + self.path,
            KEY_ENV,
            self.name,
            max_tokens=4096,
            options=options,
        )

    def catalog(self, options: dict[str, object] | None = None) -> ModelCatalog:
        """A catalog whose one model `ALIAS` is `model()` (the driver builds the same)."""
        settings: dict[str, object] = {
            "provider": self.name,
            "model": self.model_id,
            "api_key_env": KEY_ENV,
            "max_tokens": 4096,
            "base_url": UNUSED + self.path,
        }
        if options is not None:
            settings["options"] = options
        return catalog(ALIAS, **settings)

    def over(self, server: Server, environ: Mapping[str, str] = ENVIRON) -> Provider:
        """The provider, talking to `server`."""
        return self.make(server.transport, environ)

    def serving(self, text: str) -> Provider:
        """The provider, talking to a server that streams `text`."""
        return self.over(sse_text(text))


OPENAI_MODEL = "/fast/Qwen3.8-27B-absolute-heresy-Q4_K_M.gguf"
OPENAI_URL = "http://llama.test:8100/v1"
OPENAI_BARE = ModelConfig(OPENAI_MODEL, OPENAI_URL)  # no key, no max_tokens: llama.cpp's way
ANTHROPIC_MODEL = "claude-test-1"
GEMINI_MODEL = "gemini-test"
GEMINI_BARE = ModelConfig(GEMINI_MODEL, provider="gemini", api_key_env=KEY_ENV)  # no max_tokens

OPENAI = Dialect(
    "openai",
    OPENAI_MODEL,
    "/v1",
    OpenAIProvider,
    lambda t: chunk({"content": t}) + chunk({}, "stop") + DONE_LINE,
    chunk({"content": "hi"}),
    chunk({"reasoning_content": "let me "}) + chunk({"reasoning_content": "think"}),
    chunk({"content": "late"}),
    'data: {"error": {"message": "boom"}}\n\n',
    "authorization",
)
ANTHROPIC = Dialect(
    "anthropic",
    ANTHROPIC_MODEL,
    "",
    AnthropicProvider,
    lambda t: message(text(0, [t])),
    start()
    + event("ping")
    + opened(0, {"type": "text", "text": ""})
    + delta(0, type="text_delta", text="hi"),
    start()
    + opened(0, {"type": "thinking", "thinking": "", "signature": ""})
    + delta(0, type="thinking_delta", thinking="let me ")
    + delta(0, type="thinking_delta", thinking="think"),
    text(1, ["late"]),
    event("error", error={"type": "api_error", "message": "boom"}),
    "x-api-key",
)
GEMINI = Dialect(
    "gemini",
    GEMINI_MODEL,
    "/v1beta",
    GeminiProvider,
    lambda t: gemini_chunk([{"text": t}]) + gemini_chunk([], "STOP"),
    gemini_chunk([{"text": "hi"}]),
    gemini_chunk([{"text": "let me ", "thought": True}])
    + gemini_chunk([{"text": "think", "thought": True}]),
    gemini_chunk([{"text": "late"}]),
    'data: {"error": {"code": 503, "message": "boom"}}\r\n\r\n',
    "x-goog-api-key",
)
DIALECTS = {d.name: d for d in (OPENAI, ANTHROPIC, GEMINI)}
EACH = pytest.mark.parametrize("dialect", list(DIALECTS.values()), ids=list(DIALECTS))


# Walks, sampling, a driver


def walk_of(*bodies: Body, system: str = "sys") -> list[Node]:
    """The walk of a new in-memory session with `bodies` appended in order."""
    session = Session.create(model_config(ALIAS), system, 0)
    at = session.cursor(session.cursors()[0]).at
    for body in bodies:
        at = session.append(at, body, Author.Core(), 0)
    walk = session.tree.walk(at)
    session.close()
    return walk


def fresh_walk() -> list[Node]:
    """The walk of a new in-memory session: `Config`, `System`."""
    return walk_of()


def hello_walk() -> list[Node]:
    return walk_of(Body.User("hi"))


async def collect_all(
    provider: Provider,
    walk: Sequence[Node],
    model: ModelConfig | None = None,
    tools: Sequence[Tool] = (),
) -> tuple[list[Part | Finish | Delta], SampleError | None]:
    """Everything `sample` yielded, deltas included, and the `SampleError` it ended with, if any.
    `model` is the provider's dialect's own by default."""
    items: list[Part | Finish | Delta] = []
    config = model or DIALECTS[provider.name].model()
    try:
        async for item in provider.sample(walk, config, tools):
            items.append(item)
    except SampleError as e:
        return items, e
    return items, None


async def collect(
    provider: Provider,
    walk: Sequence[Node],
    model: ModelConfig | None = None,
    tools: Sequence[Tool] = (),
) -> tuple[list[Part | Finish], SampleError | None]:
    """The parts and `Finish` `sample` yielded, and the `SampleError` it ended with, if any."""
    items, error = await collect_all(provider, walk, model, tools)
    return [i for i in items if isinstance(i, Part | Finish)], error


async def posted(
    dialect: Dialect,
    walk: Sequence[Node],
    model: ModelConfig | None = None,
    tools: Sequence[Tool] = (),
    environ: Mapping[str, str] = ENVIRON,
) -> Server:
    """Sample `walk` once against a server that answers a one-word reply, and return the server
    (its `requests` and `bodies` hold what the provider sent). A sample that fails raises its
    `SampleError`; the server then holds the requests made before it."""
    server = sse_text(dialect.reply("ok"))
    provider = dialect.make(server.transport, environ)
    async for _ in provider.sample(walk, model or dialect.model(), tools):
        pass
    return server


async def sent_request(
    dialect: Dialect,
    walk: Sequence[Node],
    model: ModelConfig | None = None,
    tools: Sequence[Tool] = (),
    environ: Mapping[str, str] = ENVIRON,
) -> tuple[str, dict[str, str], dict[str, object]]:
    """The URL, headers and JSON body of the one request `posted` made."""
    server = await posted(dialect, walk, model, tools, environ)
    request = server.requests[0]
    return str(request.url), dict(request.headers), server.bodies[0]


def catalog(alias: str, **settings: object) -> ModelCatalog:
    """A catalog whose one model is `alias`, with `settings`."""
    return read_models(cli={"models": {alias: settings}})


@asynccontextmanager
async def running(
    directory: Path,
    provider: Provider,
    models: ModelCatalog | None = None,
    tools: Sequence[Tool | Callable[..., object]] = (),
) -> AsyncIterator[tuple[Session, Driver, int]]:
    """A session on disk in `directory` naming `ALIAS`, and a started driver over `provider`
    and `models` (the provider's dialect's catalog by default)."""
    session = Session.create(model_config(ALIAS), "sys", SystemClock().now(), directory)
    driver = Driver(
        session, {provider.name: provider}, models or DIALECTS[provider.name].catalog(), tools=tools
    )
    driver.start()
    try:
        yield session, driver, session.cursors()[0]
    finally:
        await driver.stop()
        session.close()


def bodies(session: Session, cursor: int) -> list[Body]:
    """The bodies on the walk to `cursor`'s node."""
    return [n.body for n in session.tree.walk(session.cursor(cursor).at)]


def sent(body: dict[str, object]) -> list[dict[str, object]]:
    """The `messages` of a request body."""
    return cast("list[dict[str, object]]", body["messages"])

"""Hand-written Messages API streams, in the event shapes of the streaming docs
(platform.claude.com/docs/en/build-with-claude/streaming), served through `httpx.MockTransport`.
Nothing here touches the network."""

import json
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from transcripts import Server

from tau_next import (
    Delta,
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
from tau_next.providers.anthropic import AnthropicProvider

NAME = "claude"
MODEL = "claude-test-1"
KEY_ENV = "TAU_TEST_ANTHROPIC_KEY"
SECRET = "sk-ant-test-0123456789"
SIGNATURE = "EqQBCgIYAhIM1gbcDa9GJwZA2b3hGgxBdjrkzLoky3dl1pkiMOYds/+=="
THINKING = "I need the GCD of 1071 and 462.\nnaïve   step: 1071 = 2 × 462 + 147"  # noqa: RUF001 - non-ASCII on purpose: replay is byte for byte
RESOLVED = ModelConfig(
    MODEL, api_key_env=KEY_ENV, provider="anthropic", max_tokens=4096
)  # what the driver resolves `NAME` to, with no options
ENVIRON = {KEY_ENV: SECRET}
HEADERS = {"content-type": "text/event-stream"}


def event(kind: str, **fields: object) -> str:
    return f"event: {kind}\ndata: {json.dumps({'type': kind, **fields})}\n\n"


def start(usage: Mapping[str, object] | None = None) -> str:
    message = {
        "id": "msg_01XFDUDYJgAACzvnptvVoYEL",
        "type": "message",
        "role": "assistant",
        "content": [],
        "model": MODEL,
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


def stream(*messages: str) -> Server:
    """A server that streams each of `messages` for each request in turn, the last repeated."""
    return Server([httpx.Response(200, headers=HEADERS, text=m) for m in messages])


def catalog(options: dict[str, object] | None = None) -> ModelCatalog:
    """A catalog whose one model `NAME` is `MODEL` on the Anthropic provider."""
    settings: dict[str, object] = {
        "provider": "anthropic",
        "model": MODEL,
        "api_key_env": KEY_ENV,
        "max_tokens": 4096,
    }
    if options is not None:
        settings["options"] = options
    return read_models(cli={"models": {NAME: settings}})


def provider(server: Server) -> AnthropicProvider:
    return AnthropicProvider(server.transport, ENVIRON)


@asynccontextmanager
async def running(
    directory: Path,
    anthropic: AnthropicProvider,
    tools: Sequence[Tool] = (),
    options: dict[str, object] | None = None,
) -> AsyncIterator[tuple[Session, Driver, int]]:
    """A session on disk in `directory` naming `NAME`, and a started driver over `anthropic`."""
    session = Session.create(model_config(NAME), "sys", SystemClock().now(), directory)
    driver = Driver(session, {"anthropic": anthropic}, catalog(options), tools=tools)
    driver.start()
    try:
        yield session, driver, session.cursors()[0]
    finally:
        await driver.stop()
        session.close()


async def sent_request(
    walk: Sequence[Node],
    model: ModelConfig = RESOLVED,
    tools: Sequence[Tool] = (),
    environ: dict[str, str] | None = None,
) -> tuple[str, dict[str, str], dict[str, object]]:
    """The URL, headers and JSON body the provider posted for one sample of `walk`, against a
    server that answers with a one-word reply. A sample that fails raises its `SampleError`."""
    server = stream(message(text(0, ["ok"])))
    anthropic = AnthropicProvider(server.transport, ENVIRON if environ is None else environ)
    async for _ in anthropic.sample(walk, model, tools):
        pass
    request = server.requests[0]
    return str(request.url), dict(request.headers), server.bodies[0]


async def collect_all(
    anthropic: AnthropicProvider,
    walk: Sequence[Node],
    model: ModelConfig = RESOLVED,
) -> tuple[list[Part | Finish | Delta], SampleError | None]:
    """Everything `sample` yielded, deltas included, and the `SampleError` it ended with, if any."""
    items: list[Part | Finish | Delta] = []
    try:
        async for item in anthropic.sample(walk, model, []):
            items.append(item)
    except SampleError as e:
        return items, e
    return items, None


async def collect(
    anthropic: AnthropicProvider,
    walk: Sequence[Node],
    model: ModelConfig = RESOLVED,
) -> tuple[list[Part | Finish], SampleError | None]:
    """The parts and `Finish` `sample` yielded, and the `SampleError` it ended with, if any."""
    items, error = await collect_all(anthropic, walk, model)
    return [i for i in items if isinstance(i, Part | Finish)], error

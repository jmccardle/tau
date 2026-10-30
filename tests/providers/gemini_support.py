"""What the Gemini tests share: a model, streams from the API's shapes, walks, a driver."""

import json
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from transcripts import FIXTURES

from tau_next import (
    Author,
    Body,
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
from tau_next.providers.gemini import GeminiProvider

KEY = "AIza-test-0123456789"
KEY_ENV = "K"
MODEL = "gemini-test"
NAME = "g"
ENVIRON = {KEY_ENV: KEY}


def model(**options: object) -> ModelConfig:
    return ModelConfig(MODEL, provider="gemini", api_key_env=KEY_ENV, options=options)


def catalog(**options: object) -> ModelCatalog:
    settings: dict[str, object] = {"provider": "gemini", "model": MODEL, "api_key_env": KEY_ENV}
    if options:
        settings["options"] = options
    return read_models(cli={"models": {NAME: settings}})


def gemini_chunk(
    parts: Sequence[Mapping[str, object]],
    finish: str | None = None,
    usage: Mapping[str, object] | None = None,
) -> str:
    """One SSE event in the API's shape: a candidate with `parts`, maybe a `finishReason`."""
    candidate: dict[str, object] = {"content": {"parts": list(parts), "role": "model"}, "index": 0}
    if finish is not None:
        candidate["finishReason"] = finish
    chunk: dict[str, object] = {"candidates": [candidate]}
    if usage is not None:
        chunk["usageMetadata"] = usage
    return f"data: {json.dumps(chunk)}\r\n\r\n"


def provider_for(text: str) -> GeminiProvider:
    """A provider whose server streams `text`."""
    return GeminiProvider(
        httpx.MockTransport(
            lambda _: httpx.Response(200, headers={"content-type": "text/event-stream"}, text=text)
        ),
        ENVIRON,
    )


def provider_sequence(*texts: str) -> tuple[GeminiProvider, list[httpx.Request]]:
    """A provider whose server streams `texts` for each request in turn (the last repeated), and
    the requests it saw."""
    seen: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        text = texts[min(len(seen), len(texts)) - 1]
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=text)

    return GeminiProvider(httpx.MockTransport(handle), ENVIRON), seen


def recorded(name: str) -> str:
    return (FIXTURES / f"{name}.sse").read_text()


def gemini_fixture(name: str) -> GeminiProvider:
    return provider_for(recorded(name))


def walk_of(*bodies: Body, system: str = "sys") -> list[Node]:
    """The walk of a new in-memory session with `bodies` appended in order."""
    session = Session.create(model_config(NAME), system, 0)
    at = session.cursor(session.cursors()[0]).at
    for body in bodies:
        at = session.append(at, body, Author.Core(), 0)
    walk = session.tree.walk(at)
    session.close()
    return walk


def gemini_walk() -> list[Node]:
    return walk_of(Body.User("hi"))


async def collect(
    provider: GeminiProvider,
    walk: Sequence[Node],
    config: ModelConfig | None = None,
    tools: Sequence[Tool] = (),
) -> tuple[list[Part | Finish | Delta], SampleError | None]:
    """Everything `sample` yielded, deltas included, and the `SampleError` it ended with, if any."""
    items: list[Part | Finish | Delta] = []
    try:
        async for item in provider.sample(walk, config or model(), tools):
            items.append(item)
    except SampleError as e:
        return items, e
    return items, None


def bodies_sent(requests: Sequence[httpx.Request]) -> list[dict[str, object]]:
    return [json.loads(r.content) for r in requests]


@asynccontextmanager
async def running(
    directory: Path, provider: GeminiProvider, tools: Sequence[Tool] = ()
) -> AsyncIterator[tuple[Session, Driver, int]]:
    """A session on disk naming `NAME`, and a started driver over `provider`."""
    session = Session.create(model_config(NAME), "sys", SystemClock().now(), directory)
    driver = Driver(session, {"gemini": provider}, catalog(), tools=tools)
    driver.start()
    try:
        yield session, driver, session.cursors()[0]
    finally:
        await driver.stop()
        session.close()


async def sent_request(
    walk: Sequence[Node],
    config: ModelConfig | None = None,
    tools: Sequence[Tool] = (),
    environ: Mapping[str, str] | None = None,
) -> tuple[str, dict[str, str], dict[str, object]]:
    """The URL, headers and JSON body the provider posted for one sample of `walk`, against a
    server that answers with a one-word reply. A sample that fails raises its `SampleError`."""
    seen: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            text=gemini_chunk([{"text": "ok"}], "STOP"),
        )

    gemini = GeminiProvider(httpx.MockTransport(handle), ENVIRON if environ is None else environ)
    async for _ in gemini.sample(walk, config or model(), tools):
        pass
    return str(seen[0].url), dict(seen[0].headers), json.loads(seen[0].content)

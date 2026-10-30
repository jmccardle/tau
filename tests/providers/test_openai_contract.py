"""The OpenAI provider's half of the D15 contract: one long-lived client, live deltas, options,
error status, and normalized usage."""

import asyncio
import json
import threading
from collections.abc import AsyncIterator, Iterator
from contextlib import contextmanager
from email.utils import formatdate
from pathlib import Path
from time import time

import httpx
import pytest
from harness import scenario
from transcripts import (
    BASE_URL,
    MODEL,
    RESOLVED,
    CutStream,
    GatedStream,
    Server,
    chunk,
    collect,
    collect_all,
    fresh_walk,
    running,
    sse,
    sse_text,
)

from tau_next import (
    Body,
    CallArgs,
    CallNamed,
    Finish,
    Live,
    ModelConfig,
    Part,
    ProviderClosed,
    SampleError,
    Stop,
    TextDelta,
    ThinkingDelta,
    read_models,
)
from tau_next.providers.openai import OpenAIProvider

DONE = "data: [DONE]\n\n"


class CountingTransport(httpx.AsyncBaseTransport):
    """A network stand-in that counts the requests it serves and the times it is closed:
    a client that is closed per sample closes its transport per sample."""

    def __init__(self, server: Server) -> None:
        self._inner = server.transport
        self.requests = 0
        self.closes = 0

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.requests += 1
        return await self._inner.handle_async_request(request)

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

    @contextmanager
    def running(self) -> Iterator[str]:
        """The base URL of the server, while it runs."""
        thread = threading.Thread(target=self._loop.run_forever, daemon=True)
        thread.start()

        async def start() -> asyncio.Server:
            return await asyncio.start_server(self._serve, "127.0.0.1", 0)

        self._server = asyncio.run_coroutine_threadsafe(start(), self._loop).result()
        port = self._server.sockets[0].getsockname()[1]
        try:
            yield f"http://127.0.0.1:{port}/v1"
        finally:
            self._loop.call_soon_threadsafe(self._server.close)
            self._loop.call_soon_threadsafe(self._loop.stop)
            thread.join()


def with_options(**options: object) -> ModelConfig:
    return ModelConfig(MODEL, BASE_URL, options=options)


def body_for(model: ModelConfig) -> dict[str, object]:
    return OpenAIProvider(environ={}).request(fresh_walk(), model, [])[2]


def provider_for(text: str) -> OpenAIProvider:
    return OpenAIProvider(sse_text(text).transport, {})


PLAIN = chunk({"content": "hi"}, "stop") + DONE


# One client


@scenario
async def test_many_samples_share_one_client_until_aclose() -> None:
    transport = CountingTransport(sse("plain"))
    provider = OpenAIProvider(transport=transport, environ={})
    for _ in range(5):
        _, error = await collect(provider, fresh_walk())
        assert error is None
    assert (transport.requests, transport.closes) == (5, 0)
    await provider.aclose()
    assert transport.closes == 1
    await provider.aclose()  # closing twice is not a failure
    assert transport.closes == 1


@scenario
async def test_two_sequential_samples_use_one_connection() -> None:
    server = KeepAliveServer(PLAIN)
    with server.running() as url:
        provider = OpenAIProvider(environ={})
        model = ModelConfig(MODEL, url)
        for _ in range(3):
            items, error = await collect(provider, fresh_walk(), model)
            assert (items, error) == ([Part.Text("hi"), Finish(Stop.End, "stop")], None)
        await provider.aclose()
    assert server.connections == 1


@pytest.mark.filterwarnings("ignore::pytest.PytestUnraisableExceptionWarning")  # the dropped client
def test_a_program_that_runs_one_event_loop_per_command_keeps_working() -> None:
    server = KeepAliveServer(PLAIN)
    with server.running() as url:
        provider = OpenAIProvider(environ={})
        model = ModelConfig(MODEL, url)
        for _ in range(2):
            items, error = asyncio.run(collect(provider, fresh_walk(), model))
            assert error is None
            assert items[0] == Part.Text("hi")
        asyncio.run(provider.aclose())


@scenario
async def test_a_sample_after_aclose_fails_as_closed_not_as_a_transient_error() -> None:
    provider = OpenAIProvider(transport=sse("plain").transport, environ={})
    await provider.aclose()
    items, error = await collect(provider, fresh_walk())
    assert items == []
    assert isinstance(error, ProviderClosed)
    assert (error.status, error.retry_after) == (None, None)


@scenario
async def test_aclose_cuts_a_sample_in_flight_as_closed() -> None:
    server = KeepAliveServer(chunk({"content": "he"}), stall=len(chunk({"content": "he"})))
    with server.running() as url:
        provider = OpenAIProvider(environ={})
        stream = provider.sample(fresh_walk(), ModelConfig(MODEL, url), [])
        assert await anext(stream) == TextDelta("he")
        rest = asyncio.ensure_future(collect_rest(stream))
        await asyncio.sleep(0.05)
        await provider.aclose()
        parts, error = await asyncio.wait_for(rest, 5)
    assert parts == [Part.Text("he")], "the text that arrived is still yielded"
    assert isinstance(error, ProviderClosed)


async def collect_rest(stream: AsyncIterator[object]) -> tuple[list[object], SampleError | None]:
    parts: list[object] = []
    try:
        async for item in stream:
            parts.append(item)
    except SampleError as e:
        return parts, e
    return parts, None


# Deltas


@scenario
async def test_text_arrives_as_deltas_then_the_part() -> None:
    items, error = await collect_all(OpenAIProvider(sse("plain").transport, {}), fresh_walk())
    assert error is None
    *deltas, part, finish = items
    assert all(isinstance(d, TextDelta) for d in deltas)
    assert "".join(d.text for d in deltas if isinstance(d, TextDelta)) == "Hello, how are you?"
    assert part == Part.Text("Hello, how are you?")
    assert isinstance(finish, Finish)


@pytest.mark.parametrize(
    "spelling",
    [
        {"reasoning_content": "hm"},
        {"reasoning": "hm"},
        {"reasoning_content": "hm", "reasoning": "hm"},
    ],
    ids=["reasoning_content", "reasoning", "both-equal"],
)
@scenario
async def test_thinking_arrives_under_either_spelling_and_the_part_precedes_the_next_kind(
    spelling: dict[str, object],
) -> None:
    text = chunk(spelling) + chunk(spelling) + chunk({"content": "ok"}, "stop") + DONE
    items, error = await collect_all(provider_for(text), fresh_walk())
    assert error is None
    assert items == [
        ThinkingDelta("hm"),
        ThinkingDelta("hm"),
        Part.Thinking("hmhm"),
        TextDelta("ok"),
        Part.Text("ok"),
        Finish(Stop.End, "stop"),
    ]


@scenario
async def test_a_delta_with_two_different_thinking_spellings_fails() -> None:
    _, error = await collect(
        provider_for(chunk({"reasoning_content": "a", "reasoning": "b"})), fresh_walk()
    )
    assert error is not None
    assert "different" in str(error)


def call_chunk(index: int | None, id_: str | None, name: str | None, args: str | None) -> str:
    function: dict[str, str] = {}
    if name is not None:
        function["name"] = name
    if args is not None:
        function["arguments"] = args
    raw: dict[str, object] = {"function": function}
    if index is not None:
        raw["index"] = index
    if id_ is not None:
        raw["id"] = id_
    return chunk({"tool_calls": [raw]})


@scenario
async def test_calls_are_named_then_their_arguments_stream() -> None:
    text = (
        call_chunk(0, "id0", "ta", "")
        + call_chunk(0, None, None, '{"x"')
        + call_chunk(0, None, None, ": 1}")
        + call_chunk(1, "id1", "tb", "{}")
        + chunk({}, "tool_calls")
    )
    items, error = await collect_all(provider_for(text), fresh_walk())
    assert error is None
    assert items == [
        CallNamed(0, "id0", "ta"),
        CallArgs(0, '{"x"'),
        CallArgs(0, ": 1}"),
        Part.Call("id0", "ta", '{"x": 1}'),
        CallNamed(1, "id1", "tb"),
        CallArgs(1, "{}"),
        Part.Call("id1", "tb", "{}"),
        Finish(Stop.Tools, "tool_calls"),
    ]


@scenario
async def test_arguments_that_arrive_before_the_name_are_held_until_it_comes() -> None:
    text = call_chunk(0, None, None, "{}") + call_chunk(0, "a", "t", None) + chunk({}, "tool_calls")
    items, error = await collect_all(provider_for(text), fresh_walk())
    assert error is None
    assert items[:3] == [CallNamed(0, "a", "t"), CallArgs(0, "{}"), Part.Call("a", "t", "{}")]


@scenario
async def test_a_name_and_id_repeated_on_every_chunk_are_not_doubled() -> None:
    text = (
        call_chunk(0, "a", "read", '{"p"')
        + call_chunk(0, "a", "read", ":1}")
        + chunk({}, "tool_calls")
    )
    items, error = await collect(provider_for(text), fresh_walk())
    assert error is None
    assert items[0] == Part.Call("a", "read", '{"p":1}')


@scenario
async def test_a_second_call_without_an_index_is_refused_not_merged() -> None:
    text = call_chunk(None, "a", "read", '{"p":1}') + call_chunk(None, "b", "read", '{"p":2}')
    _, error = await collect(provider_for(text), fresh_walk())
    assert error is not None
    assert "changed its id" in str(error)


@scenario
async def test_a_name_split_after_the_announcement_is_refused_not_cut() -> None:
    text = call_chunk(0, "a", "re", None) + call_chunk(0, None, "ad", "{}")
    _, error = await collect(provider_for(text), fresh_walk())
    assert error is not None
    assert "changed its name from 're' to 'ad'" in str(error)


@scenario
async def test_a_name_assigned_before_the_announcement_is_not_appended() -> None:
    text = call_chunk(0, None, "read", None) + call_chunk(0, None, "read", "{}")
    text += call_chunk(0, "a", None, None) + chunk({}, "tool_calls")
    items, error = await collect(provider_for(text), fresh_walk())
    assert error is None
    assert items[0] == Part.Call("a", "read", "{}")


@scenario
async def test_a_second_choice_is_refused_not_mixed_in() -> None:
    two = {"choices": [{"index": 0, "delta": {"content": "a"}}, {"index": 1, "delta": {}}]}
    _, error = await collect(provider_for(f"data: {json.dumps(two)}\n\n"), fresh_walk())
    assert error is not None
    assert "more than one choice" in str(error)
    other = {"choices": [{"index": 1, "delta": {"content": "b"}}]}
    _, error = await collect(provider_for(f"data: {json.dumps(other)}\n\n"), fresh_walk())
    assert error is not None


@scenario
async def test_a_cancel_mid_stream_leaves_an_interrupted_reply_holding_the_text(
    tmp_path: Path,
) -> None:
    gate = asyncio.Event()
    head = chunk({"reasoning_content": "hm"}) + chunk({"content": "hel"}) + chunk({"content": "lo"})
    response = httpx.Response(200, stream=GatedStream(head.encode(), gate, b""))
    provider = OpenAIProvider(Server(response).transport, {})
    async with running(tmp_path / "s", provider) as (session, driver, cursor):
        seen = driver.events(cursor)
        driver.send(cursor, "test", "hi")
        async for event in seen:
            if isinstance(event, Live) and event.delta == TextDelta("lo"):
                break
        driver.abort(cursor)
        await driver.rest(cursor)
        reply = session.tree.node(session.cursor(cursor).at).body
        await seen.aclose()
    assert isinstance(reply, Body.Assistant)
    assert reply.stop == Stop.Interrupted
    assert reply.parts == [Part.Thinking("hm"), Part.Text("hello")]


@scenario
async def test_a_driver_over_the_provider_streams_live_events_in_order(tmp_path: Path) -> None:
    text = chunk({"content": "a"}) + chunk({"content": "b"}, "stop") + DONE
    async with running(tmp_path / "s", provider_for(text)) as (_, driver, cursor):
        seen = driver.events(cursor)

        async def read() -> list[object]:
            return [e.delta async for e in seen if isinstance(e, Live)]

        reader = asyncio.ensure_future(read())  # keeps up, so no `Saved` clears a delta unread
        driver.send(cursor, "test", "hi")
        await driver.rest(cursor)
        await driver.stop()
        deltas = await reader
    assert deltas == [TextDelta("a"), TextDelta("b")]


# Options


def test_check_accepts_the_listed_options() -> None:
    model = with_options(
        temperature=0.2,
        top_p=1,
        reasoning_effort="low",
        include_usage=False,
        extra_body={"cache_prompt": True, "chain": [1, {"a": 2.5}], "stream_options": {}},
    )
    assert OpenAIProvider(environ={}).check(model) == []


@pytest.mark.parametrize(
    ("options", "message"),
    [
        ({"temperature": "hot"}, "'temperature' must be a number"),
        ({"temperature": True}, "'temperature' must be a number"),
        ({"top_p": [1]}, "'top_p' must be a number"),
        ({"reasoning_effort": 3}, "'reasoning_effort' must be a non-empty string"),
        ({"reasoning_effort": ""}, "'reasoning_effort' must be a non-empty string"),
        ({"include_usage": "yes"}, "'include_usage' must be true or false"),
        ({"extra_body": [1]}, "'extra_body' must be an object"),
        ({"extra_body": {"model": "x"}}, "may not set 'model'"),
        ({"extra_body": {"messages": []}}, "may not set 'messages'"),
        ({"extra_body": {"stream": False}}, "may not set 'stream'"),
        ({"extra_body": {"tools": []}}, "may not set 'tools'"),
        ({"extra_body": {"n": 2}}, "may not set 'n'"),
        ({"extra_body": {"stream_options": {}}}, "sets 'stream_options', which"),
        ({"temperature": 1, "extra_body": {"temperature": 0}}, "sets 'temperature', which"),
        ({"top_p": 1, "extra_body": {"top_p": 0}}, "sets 'top_p', which"),
        ({"reasoning_effort": "low", "extra_body": {"reasoning_effort": "x"}}, "sets 'reasoning"),
        ({"extra_body": {"a": {"b": [float("nan")]}}}, "holds nan"),
        ({"extra_body": {"a": float("inf")}}, "holds inf"),
        ({"seed": 1}, "unknown option 'seed'"),
    ],
)
def test_check_refuses_anything_else(options: dict[str, object], message: str) -> None:
    problems = OpenAIProvider(environ={}).check(with_options(**options))
    assert len(problems) == 1
    assert message in problems[0]


def test_extra_body_may_not_set_what_max_tokens_set() -> None:
    model = ModelConfig(MODEL, BASE_URL, max_tokens=5, options={"extra_body": {"max_tokens": 9}})
    (problem,) = OpenAIProvider(environ={}).check(model)
    assert "sets 'max_tokens', which" in problem


def test_options_reach_the_request_and_extra_body_goes_last() -> None:
    body = body_for(
        ModelConfig(
            MODEL,
            BASE_URL,
            max_tokens=10,
            options={
                "temperature": 0.5,
                "top_p": 0.9,
                "reasoning_effort": "high",
                "extra_body": {"cache_prompt": True, "chain": [{"a": 1}]},
            },
        )
    )
    assert (body["temperature"], body["top_p"], body["reasoning_effort"]) == (0.5, 0.9, "high")
    assert body["max_tokens"] == 10
    assert list(body)[-2:] == ["cache_prompt", "chain"]
    assert body["chain"] == [{"a": 1}]
    json.dumps(body)  # plain JSON, not read-only containers
    assert body["model"] == MODEL
    assert body["stream"] is True


def test_usage_is_asked_for_unless_the_option_says_no() -> None:
    assert body_for(RESOLVED)["stream_options"] == {"include_usage": True}
    assert "stream_options" not in body_for(with_options(include_usage=False))


@scenario
async def test_a_driver_refuses_a_bad_option_before_any_request(tmp_path: Path) -> None:
    server = sse("plain")
    bad = {"models": {"llama": {"model": MODEL, "base_url": BASE_URL, "options": {"seed": 1}}}}
    models = read_models(cli=bad)
    async with running(tmp_path / "s", OpenAIProvider(server.transport, {}), models) as (
        _,
        driver,
        cursor,
    ):
        driver.send(cursor, "test", "hi")
        await driver.rest(cursor)
    assert server.requests == []


# Errors


@scenario
async def test_an_http_error_carries_its_status_and_retry_after_in_seconds() -> None:
    server = Server(httpx.Response(429, headers={"Retry-After": "7"}, text="slow down"))
    _, error = await collect(OpenAIProvider(server.transport, {}), fresh_walk())
    assert error is not None
    assert (error.status, error.retry_after) == (429, 7.0)


@scenario
async def test_an_http_date_retry_after_becomes_seconds_from_now() -> None:
    when = formatdate(time() + 3600, usegmt=True)
    server = Server(httpx.Response(503, headers={"Retry-After": when}, text="busy"))
    _, error = await collect(OpenAIProvider(server.transport, {}), fresh_walk())
    assert error is not None
    assert error.status == 503
    assert error.retry_after is not None
    assert 3500 < error.retry_after <= 3600


@scenario
async def test_a_past_http_date_is_zero_seconds() -> None:
    when = formatdate(time() - 3600, usegmt=True)
    server = Server(httpx.Response(503, headers={"Retry-After": when}, text="busy"))
    _, error = await collect(OpenAIProvider(server.transport, {}), fresh_walk())
    assert error is not None
    assert error.retry_after == 0.0


@pytest.mark.parametrize("value", ["soon", "-5", "1.5", "", "2026-10-29T10:00:00Z", "9" * 400])
@scenario
async def test_any_other_retry_after_is_none(value: str) -> None:
    server = Server(httpx.Response(429, headers={"Retry-After": value}, text="no"))
    _, error = await collect(OpenAIProvider(server.transport, {}), fresh_walk())
    assert error is not None
    assert (error.status, error.retry_after) == (429, None)


@scenario
async def test_an_error_body_that_fails_to_read_still_has_its_status_and_retry_after() -> None:
    response = httpx.Response(503, headers={"Retry-After": "3"}, stream=CutStream(b"{"))
    _, error = await collect(OpenAIProvider(Server(response).transport, {}), fresh_walk())
    assert error is not None
    assert (error.status, error.retry_after) == (503, 3.0)
    assert "could not be read" in str(error)


# Usage


def usage_stream(usage: dict[str, object]) -> OpenAIProvider:
    tail = "data: " + json.dumps({"choices": [], "usage": usage}) + "\n\n"
    return provider_for(chunk({"content": "x"}, "stop") + tail + DONE)


async def usage_meta(usage: dict[str, object]) -> dict[str, object]:
    items, error = await collect(usage_stream(usage), fresh_walk())
    assert error is None
    finish = items[-1]
    assert isinstance(finish, Finish)
    assert finish.meta is not None
    return json.loads(finish.meta)


@scenario
async def test_usage_is_normalized_beside_the_servers_own_fields() -> None:
    raw: dict[str, object] = {
        "prompt_tokens": 20,
        "completion_tokens": 7,
        "total_tokens": 27,
        "prompt_tokens_details": {"cached_tokens": 12},
    }
    assert await usage_meta(raw) == {
        "usage": {"input": 20, "output": 7, "cached": 12},
        "usage_raw": raw,
    }


@pytest.mark.parametrize(
    "details",
    [{"cached_tokens": "12"}, {"cached_tokens": None}, {"cached_tokens": 1.5}, "none", [1]],
)
@scenario
async def test_a_malformed_cached_count_leaves_the_normalized_usage_out(details: object) -> None:
    raw: dict[str, object] = {
        "prompt_tokens": 3,
        "completion_tokens": 1,
        "prompt_tokens_details": details,
    }
    assert await usage_meta(raw) == {"usage_raw": raw}


@pytest.mark.parametrize("details", [None, {}])
@scenario
async def test_no_cached_count_means_zero_cached(details: object) -> None:
    raw: dict[str, object] = {
        "prompt_tokens": 3,
        "completion_tokens": 1,
        "prompt_tokens_details": details,
    }
    meta = await usage_meta(raw)
    assert meta["usage"] == {"input": 3, "output": 1, "cached": 0}

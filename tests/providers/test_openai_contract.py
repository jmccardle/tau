"""The OpenAI provider's half of the D15 contract: one long-lived client, live deltas, options,
error status, and normalized usage."""

import asyncio
import json
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


def with_options(**options: object) -> ModelConfig:
    return ModelConfig(MODEL, BASE_URL, options=options)


def body_for(model: ModelConfig) -> dict[str, object]:
    return OpenAIProvider(environ={}).request(fresh_walk(), model, [])[2]


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
async def test_a_sample_after_aclose_fails() -> None:
    provider = OpenAIProvider(transport=sse("plain").transport, environ={})
    await provider.aclose()
    items, error = await collect(provider, fresh_walk())
    assert items == []
    assert error is not None
    assert "closed" in str(error)


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


@pytest.mark.parametrize("key", ["reasoning_content", "reasoning"])
@scenario
async def test_thinking_arrives_under_either_key_and_the_part_precedes_the_next_kind(
    key: str,
) -> None:
    text = chunk({key: "hm"}) + chunk({key: "m"}) + chunk({"content": "ok"}, "stop") + DONE
    items, error = await collect_all(OpenAIProvider(sse_text(text).transport, {}), fresh_walk())
    assert error is None
    assert items == [
        ThinkingDelta("hm"),
        ThinkingDelta("m"),
        Part.Thinking("hmm"),
        TextDelta("ok"),
        Part.Text("ok"),
        Finish(Stop.End, "stop"),
    ]


@scenario
async def test_a_delta_with_both_thinking_keys_fails() -> None:
    text = chunk({"reasoning_content": "a", "reasoning": "b"})
    _, error = await collect(OpenAIProvider(sse_text(text).transport, {}), fresh_walk())
    assert error is not None
    assert "both" in str(error)


@scenario
async def test_a_call_is_named_then_its_arguments_stream() -> None:
    def call(index: int, **function: str) -> str:
        raw: dict[str, object] = {"index": index, "function": function}
        if "name" in function:
            raw["id"] = f"id{index}"
        return chunk({"tool_calls": [raw]})

    text = (
        call(0, name="ta", arguments="")
        + call(0, arguments='{"x"')
        + call(0, arguments=": 1}")
        + call(1, name="tb", arguments="{}")
        + chunk({}, "tool_calls")
    )
    items, error = await collect_all(OpenAIProvider(sse_text(text).transport, {}), fresh_walk())
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
    first = {"index": 0, "function": {"arguments": "{}"}}
    named = {"index": 0, "id": "a", "function": {"name": "t"}}
    text = chunk({"tool_calls": [first]}) + chunk({"tool_calls": [named]}) + chunk({}, "tool_calls")
    items, error = await collect_all(OpenAIProvider(sse_text(text).transport, {}), fresh_walk())
    assert error is None
    assert items[:3] == [CallNamed(0, "a", "t"), CallArgs(0, "{}"), Part.Call("a", "t", "{}")]


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
    async with running(tmp_path / "s", OpenAIProvider(sse_text(text).transport, {})) as (
        _,
        driver,
        cursor,
    ):
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
        extra_body={"cache_prompt": True, "n": [1, {"a": 2}]},
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
        ({"seed": 1}, "unknown option 'seed'"),
    ],
)
def test_check_refuses_anything_else(options: dict[str, object], message: str) -> None:
    problems = OpenAIProvider(environ={}).check(with_options(**options))
    assert len(problems) == 1
    assert message in problems[0]


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
                "extra_body": {"cache_prompt": True, "max_tokens": 99, "chain": [{"a": 1}]},
            },
        )
    )
    assert body["temperature"] == 0.5
    assert body["top_p"] == 0.9
    assert body["reasoning_effort"] == "high"
    assert body["cache_prompt"] is True
    assert body["max_tokens"] == 99, "extra_body is merged last"
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


@pytest.mark.parametrize("value", ["soon", "-5", "1.5", "", "2026-10-29T10:00:00Z"])
@scenario
async def test_any_other_retry_after_is_none(value: str) -> None:
    server = Server(httpx.Response(429, headers={"Retry-After": value}, text="no"))
    _, error = await collect(OpenAIProvider(server.transport, {}), fresh_walk())
    assert error is not None
    assert (error.status, error.retry_after) == (429, None)


@scenario
async def test_an_error_without_a_header_has_a_status_and_no_retry_after() -> None:
    server = Server(httpx.Response(500, text="boom"))
    _, error = await collect(OpenAIProvider(server.transport, {}), fresh_walk())
    assert error is not None
    assert (error.status, error.retry_after) == (500, None)


@scenario
async def test_a_connection_error_has_no_status() -> None:
    server = Server(httpx.ConnectError("refused"))
    _, error = await collect(OpenAIProvider(server.transport, {}), fresh_walk())
    assert error is not None
    assert (error.status, error.retry_after) == (None, None)


def test_a_sample_error_status_and_retry_after_are_keyword_only_and_default_to_none() -> None:
    plain = SampleError("x")
    assert (plain.status, plain.retry_after) == (None, None)
    with pytest.raises(TypeError):
        SampleError("x", None, 429)  # type: ignore[misc]
    assert SampleError("x", status=429, retry_after=2.5).retry_after == 2.5


# Usage


def usage_stream(usage: dict[str, object]) -> Server:
    tail = "data: " + json.dumps({"choices": [], "usage": usage}) + "\n\n"
    return sse_text(chunk({"content": "x"}, "stop") + tail + DONE)


@scenario
async def test_usage_is_normalized_beside_the_servers_own_fields() -> None:
    raw: dict[str, object] = {
        "prompt_tokens": 20,
        "completion_tokens": 7,
        "total_tokens": 27,
        "prompt_tokens_details": {"cached_tokens": 12},
    }
    items, error = await collect(OpenAIProvider(usage_stream(raw).transport, {}), fresh_walk())
    assert error is None
    finish = items[-1]
    assert isinstance(finish, Finish)
    assert finish.meta is not None
    assert json.loads(finish.meta) == {
        "usage": {"input": 20, "output": 7, "cached": 12},
        "usage_raw": raw,
    }


@scenario
async def test_a_server_that_reports_no_cached_tokens_has_zero_cached() -> None:
    raw: dict[str, object] = {"prompt_tokens": 3, "completion_tokens": 1}
    items, _ = await collect(OpenAIProvider(usage_stream(raw).transport, {}), fresh_walk())
    finish = items[-1]
    assert isinstance(finish, Finish)
    assert finish.meta is not None
    assert json.loads(finish.meta)["usage"] == {"input": 3, "output": 1, "cached": 0}

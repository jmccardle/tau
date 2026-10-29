"""The OpenAI provider's response handling, against recorded and hand-written streams."""

import asyncio

import httpx
import pytest
from harness import scenario
from transcripts import (
    CONFIG,
    CutStream,
    GatedStream,
    Server,
    chunk,
    collect,
    fresh_walk,
    recorded_error,
    sse,
    sse_text,
)

from tau_next import Part, Stop
from tau_next.providers.openai import OpenAIProvider

DONE = "data: [DONE]\n\n"
REASONING = (
    'User asks: "What is 17 times 3? Answer briefly." Need compute 17*3 = 51. '
    "Final should be brief.\n"
)


def provider(server: Server) -> OpenAIProvider:
    return OpenAIProvider(transport=server.transport, environ={})


@scenario
async def test_a_plain_reply_is_one_text_part_then_end() -> None:
    server = sse("plain")
    items, error = await collect(provider(server), fresh_walk())
    assert error is None
    assert items == [Part.Text("Hello, how are you?"), Stop.End]


@scenario
async def test_reasoning_content_is_a_thinking_part_before_the_text() -> None:
    items, error = await collect(provider(sse("reasoning")), fresh_walk())
    assert error is None
    assert items == [Part.Thinking(REASONING), Part.Text("51"), Stop.End]


@scenario
async def test_a_tool_call_arrives_whole_with_its_arguments_byte_for_byte() -> None:
    items, error = await collect(provider(sse("tool_call")), fresh_walk())
    assert error is None
    thinking, call, stop = items
    assert isinstance(thinking, Part.Thinking)
    assert call == Part.Call("y23a03Z82TN0yjGdHOY18qoUF5gVGfNV", "get_weather", '{"city":"Paris"}')
    assert stop == Stop.Tools


@scenario
async def test_a_part_is_yielded_once_complete_not_when_the_stream_ends() -> None:
    gate = asyncio.Event()
    head = (chunk({"reasoning_content": "hm"}) + chunk({"content": "he"})).encode()
    tail = (chunk({"content": "llo"}) + chunk({}, "stop") + DONE).encode()
    response = httpx.Response(200, stream=GatedStream(head, gate, tail))
    stream = provider(Server(response)).sample(fresh_walk(), CONFIG)
    assert await anext(stream) == Part.Thinking("hm"), "complete once the text began"
    gate.set()
    assert [item async for item in stream] == [Part.Text("hello"), Stop.End]


@scenario
async def test_parallel_calls_are_separate_parts_in_order() -> None:
    def call(index: int, id_: str, name: str, args: str) -> str:
        function = {"name": name, "arguments": args}
        return chunk({"tool_calls": [{"index": index, "id": id_, "function": function}]})

    text = (
        call(0, "a", "ta", '{"x"')
        + chunk({"tool_calls": [{"index": 0, "function": {"arguments": ": 1}"}}]})
        + call(1, "b", "tb", "{}")
        + chunk({}, "tool_calls")
    )
    items, error = await collect(provider(sse_text(text)), fresh_walk())
    assert error is None
    assert items == [Part.Call("a", "ta", '{"x": 1}'), Part.Call("b", "tb", "{}"), Stop.Tools]


@pytest.mark.parametrize(
    ("finish", "stop"),
    [
        ("stop", Stop.End),
        ("tool_calls", Stop.Tools),
        ("length", Stop.Length),
        ("content_filter", Stop.Refused),
    ],
)
@scenario
async def test_finish_reasons_normalize_to_stops(finish: str, stop: Stop) -> None:
    server = sse_text(chunk({"content": "x"}, finish))
    items, error = await collect(provider(server), fresh_walk())
    assert (items, error) == ([Part.Text("x"), stop], None)


@scenario
async def test_sse_comments_other_fields_and_missing_spaces_are_tolerated() -> None:
    event = chunk({"content": "x"}, "stop").removeprefix("data: ")
    server = sse_text(f": keep-alive\n\nevent: message\ndata:{event}")
    items, error = await collect(provider(server), fresh_walk())
    assert (items, error) == ([Part.Text("x"), Stop.End], None)


@scenario
async def test_an_http_error_fails_the_sample_with_the_servers_message() -> None:
    items, error = await collect(provider(recorded_error()), fresh_walk())
    assert items == []
    assert error is not None
    assert str(error).startswith("HTTP 500: ")
    assert "No user query found in messages" in str(error)


@scenario
async def test_a_non_json_error_body_is_quoted() -> None:
    server = Server(httpx.Response(429, text="slow down"))
    items, error = await collect(provider(server), fresh_walk())
    assert (items, str(error)) == ([], "HTTP 429: slow down")


@scenario
async def test_a_refused_connection_fails_the_sample() -> None:
    server = Server(httpx.ConnectError("connection refused"))
    items, error = await collect(provider(server), fresh_walk())
    assert items == []
    assert str(error) == "ConnectError: connection refused"


@scenario
async def test_a_connection_reset_mid_stream_keeps_the_parts_so_far() -> None:
    head = (chunk({"reasoning_content": "hm"}) + chunk({"content": "par"})).encode()
    server = Server(httpx.Response(200, stream=CutStream(head)))
    items, error = await collect(provider(server), fresh_walk())
    assert items == [Part.Thinking("hm"), Part.Text("par")]
    assert error is not None
    assert "ReadError" in str(error)


@scenario
async def test_a_stream_that_ends_without_a_finish_fails_and_keeps_its_text() -> None:
    server = sse_text(chunk({"content": "hi"}) + DONE)
    items, error = await collect(provider(server), fresh_walk())
    assert items == [Part.Text("hi")]
    assert error is not None
    assert "finish_reason" in str(error)


@scenario
async def test_a_stream_that_ends_without_done_after_its_finish_is_complete() -> None:
    items, error = await collect(provider(sse_text(chunk({"content": "x"}, "stop"))), fresh_walk())
    assert (items, error) == ([Part.Text("x"), Stop.End], None)


@pytest.mark.parametrize(
    ("body", "message"),
    [
        ("data: {not json\n\n", "malformed SSE data"),
        ("data: [1, 2]\n\n", "not a JSON object"),
        ('data: {"id": "x"}\n\n', "no 'choices'"),
        ('data: {"error": {"message": "overloaded"}}\n\n', "overloaded"),
        (chunk({"content": "x"}, "mystery"), "unknown finish_reason 'mystery'"),
        (
            chunk({"tool_calls": [{"index": 0, "function": {"arguments": "{}"}}]}, "tool_calls"),
            "no id or no name",
        ),
    ],
)
@scenario
async def test_malformed_streams_fail_the_sample(body: str, message: str) -> None:
    _, error = await collect(provider(sse_text(body)), fresh_walk())
    assert error is not None
    assert message in str(error)


@scenario
async def test_a_call_that_resumes_after_another_began_fails() -> None:
    def call(index: int) -> str:
        return chunk(
            {"tool_calls": [{"index": index, "id": f"c{index}", "function": {"name": "t"}}]}
        )

    _, error = await collect(provider(sse_text(call(0) + call(1) + call(0))), fresh_walk())
    assert error is not None
    assert "continues after it was complete" in str(error)

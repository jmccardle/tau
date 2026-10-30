"""The OpenAI provider's response handling, against recorded and hand-written streams."""

import asyncio
import json

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

from tau_next import Finish, Part, Stop
from tau_next.providers.openai import OpenAIProvider

DONE = "data: [DONE]\n\n"
REASONING = (
    'User asks: "What is 17 times 3? Answer briefly." Need compute 17*3 = 51. '
    "Final should be brief.\n"
)


def meta_of(item: object) -> object:
    assert isinstance(item, Finish)
    assert item.meta is not None
    return json.loads(item.meta)


def provider(server: Server) -> OpenAIProvider:
    return OpenAIProvider(transport=server.transport, environ={})


@scenario
async def test_a_plain_reply_is_one_text_part_then_end() -> None:
    server = sse("plain")
    items, error = await collect(provider(server), fresh_walk())
    assert error is None
    assert items[:-1] == [Part.Text("Hello, how are you?")]
    finish = items[-1]
    assert isinstance(finish, Finish)
    assert (finish.stop, finish.reason) == (Stop.End, "stop")
    assert meta_of(finish) == {
        "model": "/fast/Qwen3.8-27B-absolute-heresy-Q4_K_M.gguf",
        "id": "chatcmpl-PDYpQXnYl6gO6gM1pARqC6R7yqmMGiDs",
        "system_fingerprint": "b1637-9c7a7553",
    }, "only what the server sent: the recording has no usage"


@scenario
async def test_usage_arrives_after_the_finish_and_joins_the_meta() -> None:
    usage = {"prompt_tokens": 20, "completion_tokens": 7, "total_tokens": 27}
    text = (
        chunk({"content": "x"}, "stop")
        + "data: "
        + json.dumps({"id": "c1", "model": "m", "choices": [], "usage": usage})
        + "\n\n"
        + DONE
    )
    items, error = await collect(provider(sse_text(text)), fresh_walk())
    assert error is None
    assert meta_of(items[-1]) == {"id": "c1", "model": "m", "usage": usage}


@scenario
async def test_a_response_that_says_nothing_about_itself_has_no_meta() -> None:
    items, error = await collect(provider(sse_text(chunk({"content": "x"}, "stop"))), fresh_walk())
    assert error is None
    assert items[-1] == Finish(Stop.End, "stop", None)


def test_a_finish_meta_that_is_not_a_json_object_is_refused() -> None:
    for bad in ("not json", "[1]", '"s"', "3"):
        with pytest.raises(ValueError, match="JSON object"):
            Finish(Stop.End, "stop", bad)


@scenario
async def test_reasoning_content_is_a_thinking_part_before_the_text() -> None:
    items, error = await collect(provider(sse("reasoning")), fresh_walk())
    assert error is None
    assert items[:-1] == [Part.Thinking(REASONING), Part.Text("51")]
    finish = items[-1]
    assert isinstance(finish, Finish)
    assert (finish.stop, finish.reason) == (Stop.End, "stop")


@scenario
async def test_a_tool_call_arrives_whole_with_its_arguments_byte_for_byte() -> None:
    items, error = await collect(provider(sse("tool_call")), fresh_walk())
    assert error is None
    thinking, call, finish = items
    assert isinstance(thinking, Part.Thinking)
    assert call == Part.Call("y23a03Z82TN0yjGdHOY18qoUF5gVGfNV", "get_weather", '{"city":"Paris"}')
    assert isinstance(finish, Finish)
    assert (finish.stop, finish.reason) == (Stop.Tools, "tool_calls")


@scenario
async def test_a_part_is_yielded_once_complete_not_when_the_stream_ends() -> None:
    gate = asyncio.Event()
    head = (chunk({"reasoning_content": "hm"}) + chunk({"content": "he"})).encode()
    tail = (chunk({"content": "llo"}) + chunk({}, "stop") + DONE).encode()
    response = httpx.Response(200, stream=GatedStream(head, gate, tail))
    stream = provider(Server(response)).sample(fresh_walk(), CONFIG, [])
    assert await anext(stream) == Part.Thinking("hm"), "complete once the text began"
    gate.set()
    assert [item async for item in stream] == [Part.Text("hello"), Finish(Stop.End, "stop")]


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
    assert items == [
        Part.Call("a", "ta", '{"x": 1}'),
        Part.Call("b", "tb", "{}"),
        Finish(Stop.Tools, "tool_calls"),
    ]


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
    assert (items, error) == ([Part.Text("x"), Finish(stop, finish)], None)


@scenario
async def test_sse_comments_other_fields_and_missing_spaces_are_tolerated() -> None:
    event = chunk({"content": "x"}, "stop").removeprefix("data: ")
    server = sse_text(f": keep-alive\n\nevent: message\ndata:{event}")
    items, error = await collect(provider(server), fresh_walk())
    assert (items, error) == ([Part.Text("x"), Finish(Stop.End, "stop")], None)


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
    assert (items, error) == ([Part.Text("x"), Finish(Stop.End, "stop")], None)


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

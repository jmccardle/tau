"""The OpenAI provider's response handling, against recorded and hand-written streams."""

import asyncio
import json

import httpx
import pytest
from harness import scenario
from wire import (
    DONE_LINE,
    OPENAI,
    GatedStream,
    Server,
    chunk,
    collect,
    cut,
    fresh_walk,
    recorded_error,
    sse,
)

from tau import Finish, Part, SampleError, Stop, TextDelta, ThinkingDelta


def meta_of(item: object) -> object:
    assert isinstance(item, Finish)
    assert item.meta is not None
    return json.loads(item.meta)


@scenario
async def test_usage_arrives_after_the_finish_and_joins_the_meta() -> None:
    usage = {"prompt_tokens": 20, "completion_tokens": 7, "total_tokens": 27}
    text = (
        chunk({"content": "x"}, "stop")
        + "data: "
        + json.dumps({"id": "c1", "model": "m", "choices": [], "usage": usage})
        + "\n\n"
        + DONE_LINE
    )
    items, error = await collect(OPENAI.serving(text), fresh_walk())
    assert error is None
    assert meta_of(items[-1]) == {
        "id": "c1",
        "model": "m",
        "usage": {"input": 20, "output": 7, "cached": 0},
        "usage_raw": usage,
    }


@scenario
async def test_the_llama_cpp_usage_chunk_joins_the_meta_and_timings_do_not() -> None:
    """`plain_usage.sse` is `plain.sse` plus llama.cpp's trailing chunk: empty `choices`,
    `usage`, `timings`, then `[DONE]`."""
    items, error = await collect(OPENAI.over(sse("plain_usage")), fresh_walk())
    assert error is None
    assert items[:-1] == [Part.Text("Hello, how are you?")]
    assert meta_of(items[-1]) == {
        "model": "/fast/Qwen3.8-27B-absolute-heresy-Q4_K_M.gguf",
        "id": "chatcmpl-PDYpQXnYl6gO6gM1pARqC6R7yqmMGiDs",
        "system_fingerprint": "b1637-9c7a7553",
        "usage": {"input": 20, "output": 7, "cached": 0},
        "usage_raw": {"completion_tokens": 7, "prompt_tokens": 20, "total_tokens": 27},
    }


@scenario
async def test_a_usage_chunk_without_a_choices_key_is_not_an_error() -> None:
    usage = {"total_tokens": 5}
    text = chunk({"content": "x"}, "stop") + f"data: {json.dumps({'usage': usage})}\n\n" + DONE_LINE
    items, error = await collect(OPENAI.serving(text), fresh_walk())
    assert error is None
    assert meta_of(items[-1]) == {"usage_raw": usage}, "no token counts, no normalized usage"


@scenario
async def test_an_error_before_the_finish_still_fails_and_carries_the_meta_so_far() -> None:
    text = 'data: {"id":"c1","choices":[{"index":0,"delta":{"content":"p"}}]}\n\n' + (
        'data: {"error":{"message":"boom"}}\n\n'
    )
    items, error = await collect(OPENAI.serving(text), fresh_walk())
    assert items == [Part.Text("p")]
    assert error is not None
    assert "boom" in str(error)
    assert error.meta is not None
    assert json.loads(error.meta) == {"id": "c1"}


def test_a_finish_meta_that_is_not_a_json_object_is_refused() -> None:
    for bad in ("not json", "[1]", '"s"', "3", '{"a": NaN}', '{"a": Infinity}', '{"a": -Infinity}'):
        with pytest.raises(ValueError, match="JSON"):
            Finish(Stop.End, "stop", bad)


def test_a_finish_meta_that_is_not_text_is_a_type_error() -> None:
    for bad in (b'{"a": 1}', {"a": 1}, 3):
        with pytest.raises(TypeError, match="meta must be"):
            Finish(Stop.End, "stop", bad)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="meta must be"):
        SampleError("x", b"{}")  # type: ignore[arg-type]


@scenario
async def test_a_tool_call_arrives_whole_with_its_arguments_byte_for_byte() -> None:
    items, error = await collect(OPENAI.over(sse("tool_call")), fresh_walk())
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
    tail = (chunk({"content": "llo"}) + chunk({}, "stop") + DONE_LINE).encode()
    response = httpx.Response(200, stream=GatedStream(head, gate, tail))
    stream = OPENAI.over(Server(response)).sample(fresh_walk(), OPENAI.model(), [])
    assert await anext(stream) == ThinkingDelta("hm")
    assert await anext(stream) == Part.Thinking("hm"), "complete once the text began"
    assert await anext(stream) == TextDelta("he")
    gate.set()
    assert [item async for item in stream] == [
        TextDelta("llo"),
        Part.Text("hello"),
        Finish(Stop.End, "stop"),
    ]


@pytest.mark.parametrize(
    ("finish", "stop"),
    [
        ("stop", Stop.End),
        ("tool_calls", Stop.Tools),
        ("length", Stop.Length),
        ("content_filter", Stop.End),
    ],
)
@scenario
async def test_finish_reasons_normalize_to_stops(finish: str, stop: Stop) -> None:
    items, error = await collect(OPENAI.serving(chunk({"content": "x"}, finish)), fresh_walk())
    assert (items, error) == ([Part.Text("x"), Finish(stop, finish)], None)


@scenario
async def test_sse_comments_other_fields_and_missing_spaces_are_tolerated() -> None:
    event = chunk({"content": "x"}, "stop").removeprefix("data: ")
    text = f": keep-alive\n\nevent: message\ndata:{event}"
    items, error = await collect(OPENAI.serving(text), fresh_walk())
    assert (items, error) == ([Part.Text("x"), Finish(Stop.End, "stop")], None)


@scenario
async def test_an_http_error_fails_the_sample_with_the_servers_message() -> None:
    items, error = await collect(OPENAI.over(recorded_error()), fresh_walk())
    assert items == []
    assert error is not None
    assert error.status == 500
    assert "No user query found in messages" in str(error)


@scenario
async def test_a_non_json_error_body_is_quoted() -> None:
    server = Server(httpx.Response(429, text="slow down"))
    items, error = await collect(OPENAI.over(server), fresh_walk())
    assert items == []
    assert error is not None
    assert (error.status, error.retry_after) == (429, None)
    assert "slow down" in str(error)


@scenario
async def test_a_connection_reset_mid_stream_keeps_the_parts_so_far() -> None:
    server = cut(chunk({"reasoning_content": "hm"}) + chunk({"content": "par"}))
    items, error = await collect(OPENAI.over(server), fresh_walk())
    assert items == [Part.Thinking("hm"), Part.Text("par")]
    assert error is not None
    assert "ReadError" in str(error)


@scenario
async def test_a_stream_that_ends_without_a_finish_fails_and_keeps_its_text() -> None:
    items, error = await collect(OPENAI.serving(chunk({"content": "hi"}) + DONE_LINE), fresh_walk())
    assert items == [Part.Text("hi")]
    assert error is not None
    assert "finish_reason" in str(error)


@pytest.mark.parametrize(
    ("body", "message"),
    [
        ("data: {not json\n\n", "malformed SSE data"),
        ("data: [1, 2]\n\n", "not a JSON object"),
        ('data: {"id": "x"}\n\n', "choices"),
        (chunk({"content": "x"}, "mystery"), "unknown finish_reason 'mystery'"),
    ],
)
@scenario
async def test_malformed_streams_fail_the_sample(body: str, message: str) -> None:
    _, error = await collect(OPENAI.serving(body), fresh_walk())
    assert error is not None
    assert message in str(error)


@scenario
async def test_a_call_that_resumes_after_another_began_fails() -> None:
    def call(index: int) -> str:
        return chunk(
            {"tool_calls": [{"index": index, "id": f"c{index}", "function": {"name": "t"}}]}
        )

    _, error = await collect(OPENAI.serving(call(0) + call(1) + call(0)), fresh_walk())
    assert error is not None
    assert "continues after it was complete" in str(error)


@pytest.mark.parametrize("name", [None, "read"])
@scenario
async def test_a_call_cut_off_is_dropped_and_its_text_and_name_go_in_the_error(
    name: str | None,
) -> None:
    function = {"arguments": '{"ci'} | ({} if name is None else {"name": name})
    delta: dict[str, object] = {"tool_calls": [{"index": 0, "function": function}]}
    items, error = await collect(OPENAI.over(cut(chunk(delta))), fresh_walk())
    assert items == []
    assert error is not None
    assert "a call was cut off and dropped" in str(error)
    assert '{"ci' in str(error), "the raw argument text is kept"
    assert name is None or repr(name) in str(error)

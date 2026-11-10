"""The OpenAI provider's half of the D15 contract: live deltas, options, error status, and
normalized usage."""

import json
from email.utils import formatdate
from time import time

import httpx
import pytest
from harness import scenario
from wire import (
    DONE_LINE,
    OPENAI,
    OPENAI_BARE,
    OPENAI_MODEL,
    OPENAI_URL,
    CutStream,
    Server,
    chunk,
    collect,
    collect_all,
    fresh_walk,
    posted,
    sse,
)

from tau import (
    CallArgs,
    CallNamed,
    Finish,
    ModelConfig,
    OpenAIProvider,
    Part,
    Provider,
    Stop,
    TextDelta,
    ThinkingDelta,
)


def with_options(**options: object) -> ModelConfig:
    return ModelConfig(OPENAI_MODEL, OPENAI_URL, options=options)


async def body_for(model: ModelConfig) -> dict[str, object]:
    return (await posted(OPENAI, fresh_walk(), model, environ={})).bodies[0]


# Deltas


@scenario
async def test_text_arrives_as_deltas_then_the_part_then_the_finish_with_what_the_server_said() -> (
    None
):
    items, error = await collect_all(OPENAI.over(sse("plain")), fresh_walk())
    assert error is None
    *deltas, part, finish = items
    assert all(isinstance(d, TextDelta) for d in deltas)
    assert "".join(d.text for d in deltas if isinstance(d, TextDelta)) == "Hello, how are you?"
    assert part == Part.Text("Hello, how are you?")
    assert isinstance(finish, Finish)
    assert (finish.stop, finish.reason) == (Stop.End, "stop")
    assert finish.meta is not None
    meta = json.loads(finish.meta)
    timings = meta.pop("timings")
    assert (timings["prompt_n"], timings["cache_n"]) == (20, 0), "llama.cpp's timings are kept"
    assert meta == {
        "model": OPENAI_MODEL,
        "id": "chatcmpl-PDYpQXnYl6gO6gM1pARqC6R7yqmMGiDs",
        "system_fingerprint": "b1637-9c7a7553",
    }, "only what the server sent: the recording has no usage"


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
    text = chunk(spelling) + chunk(spelling) + chunk({"content": "ok"}, "stop") + DONE_LINE
    items, error = await collect_all(OPENAI.serving(text), fresh_walk())
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
    text = chunk({"reasoning_content": "a", "reasoning": "b"})
    _, error = await collect(OPENAI.serving(text), fresh_walk())
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
    items, error = await collect_all(OPENAI.serving(text), fresh_walk())
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
    items, error = await collect_all(OPENAI.serving(text), fresh_walk())
    assert error is None
    assert items[:3] == [CallNamed(0, "a", "t"), CallArgs(0, "{}"), Part.Call("a", "t", "{}")]


@scenario
async def test_a_name_and_id_repeated_on_every_chunk_are_not_doubled() -> None:
    text = (
        call_chunk(0, "a", "read", '{"p"')
        + call_chunk(0, "a", "read", ":1}")
        + chunk({}, "tool_calls")
    )
    items, error = await collect(OPENAI.serving(text), fresh_walk())
    assert error is None
    assert items[0] == Part.Call("a", "read", '{"p":1}')


@scenario
async def test_a_second_call_without_an_index_is_refused_not_merged() -> None:
    text = call_chunk(None, "a", "read", '{"p":1}') + call_chunk(None, "b", "read", '{"p":2}')
    _, error = await collect(OPENAI.serving(text), fresh_walk())
    assert error is not None
    assert "changed its id" in str(error)


@scenario
async def test_a_name_split_after_the_announcement_is_refused_not_cut() -> None:
    text = call_chunk(0, "a", "re", None) + call_chunk(0, None, "ad", "{}")
    _, error = await collect(OPENAI.serving(text), fresh_walk())
    assert error is not None
    assert "changed its name from 're' to 'ad'" in str(error)


@scenario
async def test_a_name_assigned_before_the_announcement_is_not_appended() -> None:
    text = call_chunk(0, None, "read", None) + call_chunk(0, None, "read", "{}")
    text += call_chunk(0, "a", None, None) + chunk({}, "tool_calls")
    items, error = await collect(OPENAI.serving(text), fresh_walk())
    assert error is None
    assert items[0] == Part.Call("a", "read", "{}")


@scenario
async def test_a_second_choice_is_refused_not_mixed_in() -> None:
    two = {"choices": [{"index": 0, "delta": {"content": "a"}}, {"index": 1, "delta": {}}]}
    _, error = await collect(OPENAI.serving(f"data: {json.dumps(two)}\n\n"), fresh_walk())
    assert error is not None
    assert "more than one choice" in str(error)
    other = {"choices": [{"index": 1, "delta": {"content": "b"}}]}
    _, error = await collect(OPENAI.serving(f"data: {json.dumps(other)}\n\n"), fresh_walk())
    assert error is not None


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
        ({"temperature": 10**400}, "'temperature' must be a number"),
        ({"reasoning_effort": ""}, "'reasoning_effort' must be a non-empty string"),
        ({"include_usage": "yes"}, "'include_usage' must be true or false"),
        ({"extra_body": [1]}, "'extra_body' must be an object"),
        ({"extra_body": {"model": "x"}}, "may not set 'model'"),
        ({"extra_body": {"stream_options": {}}}, "sets 'stream_options', which"),
        ({"temperature": 1, "extra_body": {"temperature": 0}}, "sets 'temperature', which"),
        ({"extra_body": {"a": {"b": [float("nan")]}}}, "holds nan"),
        ({"seed": 1}, "unknown option 'seed'"),
    ],
)
def test_check_refuses_anything_else(options: dict[str, object], message: str) -> None:
    problems = OpenAIProvider(environ={}).check(with_options(**options))
    assert len(problems) == 1
    assert message in problems[0]


def test_extra_body_may_not_set_what_max_tokens_set() -> None:
    model = ModelConfig(
        OPENAI_MODEL, OPENAI_URL, max_tokens=5, options={"extra_body": {"max_tokens": 9}}
    )
    (problem,) = OpenAIProvider(environ={}).check(model)
    assert "sets 'max_tokens', which" in problem


@scenario
async def test_options_reach_the_request_with_extra_body_beside_them() -> None:
    body = await body_for(
        ModelConfig(
            OPENAI_MODEL,
            OPENAI_URL,
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
    assert (body["cache_prompt"], body["chain"]) == (True, [{"a": 1}])
    assert body["max_tokens"] == 10
    assert (body["model"], body["stream"]) == (OPENAI_MODEL, True)


@scenario
async def test_usage_is_asked_for_unless_the_option_says_no() -> None:
    assert (await body_for(OPENAI_BARE))["stream_options"] == {"include_usage": True}
    assert "stream_options" not in await body_for(with_options(include_usage=False))


# Errors


@pytest.mark.parametrize(
    ("value", "wait"),
    [
        ("7", 7.0),
        ("0", 0.0),
        ("soon", None),
        ("-5", None),
        ("1.5", None),
        ("", None),
        ("2026-10-29T10:00:00Z", None),
        ("9" * 400, None),
    ],
)
@scenario
async def test_retry_after_is_whole_seconds_or_none(value: str, wait: float | None) -> None:
    server = Server(httpx.Response(429, headers={"Retry-After": value}, text="slow down"))
    _, error = await collect(OPENAI.over(server), fresh_walk())
    assert error is not None
    assert (error.status, error.retry_after) == (429, wait)


@pytest.mark.parametrize(("offset", "low", "high"), [(3600, 3500, 3600), (-3600, 0, 0)])
@scenario
async def test_an_http_date_retry_after_becomes_seconds_from_now_or_zero_if_past(
    offset: int, low: float, high: float
) -> None:
    when = formatdate(time() + offset, usegmt=True)
    server = Server(httpx.Response(503, headers={"Retry-After": when}, text="busy"))
    _, error = await collect(OPENAI.over(server), fresh_walk())
    assert error is not None
    assert error.status == 503
    assert error.retry_after is not None
    assert low <= error.retry_after <= high


@scenario
async def test_an_error_body_that_fails_to_read_still_has_its_status_and_retry_after() -> None:
    response = httpx.Response(503, headers={"Retry-After": "3"}, stream=CutStream(b"{"))
    _, error = await collect(OPENAI.over(Server(response)), fresh_walk())
    assert error is not None
    assert (error.status, error.retry_after) == (503, 3.0)
    assert "could not be read" in str(error)


# Usage


def usage_stream(usage: dict[str, object]) -> Provider:
    tail = "data: " + json.dumps({"choices": [], "usage": usage}) + "\n\n"
    return OPENAI.serving(chunk({"content": "x"}, "stop") + tail + DONE_LINE)


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
async def test_no_cached_count_means_no_cached_key(details: object) -> None:
    """A server that says nothing has not said it read nothing: a cache notice must tell a miss
    (`cached: 0`) from silence (D24 section 3)."""
    raw: dict[str, object] = {
        "prompt_tokens": 3,
        "completion_tokens": 1,
        "prompt_tokens_details": details,
    }
    meta = await usage_meta(raw)
    assert meta["usage"] == {"input": 3, "output": 1}


@scenario
async def test_a_reported_zero_is_a_miss_not_silence() -> None:
    raw: dict[str, object] = {
        "prompt_tokens": 3,
        "completion_tokens": 1,
        "prompt_tokens_details": {"cached_tokens": 0},
    }
    assert (await usage_meta(raw))["usage"] == {"input": 3, "output": 1, "cached": 0}


@scenario
async def test_a_litellm_proxy_usage_for_an_anthropic_model_has_reads_and_writes() -> None:
    """What LiteLLM 1.106's OpenAI endpoint sends for an Anthropic model (read from its source,
    `Usage.__init__` and `calculate_usage`): `prompt_tokens` counts reads and writes, the
    details always hold `cached_tokens` (0 on a miss) and `cache_write_tokens`, and the same
    writes ride at the top level as `cache_creation_input_tokens`."""
    raw: dict[str, object] = {
        "prompt_tokens": 2112,
        "completion_tokens": 77,
        "total_tokens": 2189,
        "prompt_tokens_details": {
            "cached_tokens": 2000,
            "cache_write_tokens": 100,
            "cache_creation_tokens": 100,
        },
        "cache_creation_input_tokens": 100,
        "cache_read_input_tokens": 2000,
    }
    meta = await usage_meta(raw)
    assert meta["usage"] == {"input": 2112, "output": 77, "cached": 2000, "written": 100}


@pytest.mark.parametrize(
    "details",
    [{"cache_write_tokens": "1"}, {"cache_write_tokens": None}, {"cache_write_tokens": -1}],
)
@scenario
async def test_a_malformed_written_count_leaves_the_normalized_usage_out(details: object) -> None:
    raw: dict[str, object] = {
        "prompt_tokens": 3,
        "completion_tokens": 1,
        "prompt_tokens_details": details,
    }
    assert await usage_meta(raw) == {"usage_raw": raw}


@pytest.mark.parametrize(
    "details",
    [
        {"cached_tokens": 4},
        {"cache_write_tokens": 4},
        {"cached_tokens": 2, "cache_write_tokens": 2},
    ],
)
@scenario
async def test_more_read_and_written_than_sent_is_as_malformed_as_a_bad_count(
    details: object,
) -> None:
    raw: dict[str, object] = {
        "prompt_tokens": 3,
        "completion_tokens": 1,
        "prompt_tokens_details": details,
    }
    assert await usage_meta(raw) == {"usage_raw": raw}

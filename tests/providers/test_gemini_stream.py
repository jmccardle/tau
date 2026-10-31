"""The Gemini provider's response handling: streams recorded from the live API (key never in
them; `fixtures/gemini_*.sse`) and ones written from the API reference's shapes. Each ends in
the request that sends what it read back, where the two halves must agree."""

import json

import httpx
import pytest
from fakes import FakeTool
from harness import scenario
from wire import (
    FIXTURES,
    GEMINI,
    GEMINI_BARE,
    Server,
    collect,
    collect_all,
    gemini_chunk,
    hello_walk,
    recorded,
    sent_request,
    walk_of,
)

from tau import (
    Author,
    Body,
    CallArgs,
    CallNamed,
    Finish,
    Outcome,
    Part,
    Replay,
    Stop,
    TextDelta,
    ThinkingDelta,
)


def recorded_signature(name: str) -> str:
    """The `thoughtSignature` of the last chunk in a recorded stream that has one."""
    found = [
        part["thoughtSignature"]
        for line in (FIXTURES / name).read_text().splitlines()
        if line.startswith("data:")
        for candidate in json.loads(line[5:]).get("candidates", [])
        for part in candidate["content"].get("parts", [])
        if "thoughtSignature" in part
    ]
    return found[-1]


def replay(**data: str) -> Replay:
    return Replay("gemini", json.dumps(data))


@scenario
async def test_thoughts_then_chunked_text_are_two_parts_and_the_signature_lands_on_the_text() -> (
    None
):
    """Recorded: four thought chunks, then three text chunks, then a last chunk that carries
    the signature (and the finish reason). The text is one part holding all of them."""
    items, error = await collect_all(GEMINI.serving(recorded("gemini_text_thinking")), hello_walk())
    assert error is None
    deltas = [i for i in items if isinstance(i, TextDelta | ThinkingDelta)]
    parts = [i for i in items if isinstance(i, Part)]
    assert [type(p) for p in parts] == [Part.Thinking, Part.Text]
    thinking, text = parts
    assert isinstance(thinking, Part.Thinking)
    assert isinstance(text, Part.Text)
    assert thinking.replay is None, "the thought summaries carried no signature"
    assert thinking.text == "".join(d.text for d in deltas if isinstance(d, ThinkingDelta))
    assert text.text == "".join(d.text for d in deltas if isinstance(d, TextDelta))
    assert text.replay == replay(thoughtSignature=recorded_signature("gemini_text_thinking.sse"))
    kinds = [type(d) for d in deltas]
    assert kinds == sorted(kinds, key=lambda k: k is TextDelta), "thinking first, then text"
    order = [i for i in items if isinstance(i, Part | TextDelta | ThinkingDelta)]
    assert order.index(thinking) < order.index(next(d for d in deltas if isinstance(d, TextDelta)))


@scenario
async def test_the_finish_normalizes_the_stop_and_the_usage_counts_thoughts_as_output() -> None:
    items, _ = await collect(GEMINI.serving(recorded("gemini_text_thinking")), hello_walk())
    finish = items[-1]
    assert isinstance(finish, Finish)
    assert (finish.stop, finish.reason) == (Stop.Length, "MAX_TOKENS")
    assert finish.meta is not None
    meta = json.loads(finish.meta)
    assert meta["usage"] == {"input": 19, "output": 59 + 1437, "cached": 0}
    assert meta["usage_raw"]["thoughtsTokenCount"] == 1437
    assert meta["model"] == "gemini-3.6-flash"


@scenario
async def test_cached_tokens_come_from_cachedContentTokenCount() -> None:
    usage = {"promptTokenCount": 100, "cachedContentTokenCount": 60, "candidatesTokenCount": 5}
    text = gemini_chunk([{"text": "x"}], "STOP", usage)
    items, _ = await collect(GEMINI.serving(text), hello_walk())
    finish = items[-1]
    assert isinstance(finish, Finish)
    assert finish.meta is not None
    assert json.loads(finish.meta)["usage"] == {"input": 100, "output": 5, "cached": 60}


@scenario
async def test_a_call_arrives_whole_and_the_api_id_and_signature_are_kept() -> None:
    items, error = await collect_all(GEMINI.serving(recorded("gemini_call")), hello_walk())
    assert error is None
    args = '{"path": "/tmp/x.txt"}'
    signature = recorded_signature("gemini_call.sse")
    assert items[:3] == [CallNamed(0, "call_698416", "read"), CallArgs(0, args), items[2]]
    assert items[2] == Part.Call(
        "call_698416", "read", args, replay(id="call_698416", thoughtSignature=signature)
    )
    finish = items[-1]
    assert isinstance(finish, Finish)
    assert (finish.stop, finish.reason) == (Stop.Tools, "STOP"), "STOP with a call is Tools"


@scenario
async def test_a_call_with_no_id_is_given_a_unique_one_that_is_never_sent_back() -> None:
    bare = {"functionCall": {"name": "ta", "args": {"p": 1}}}
    named = {"functionCall": {"name": "tb", "args": {}, "id": "api-1"}, "thoughtSignature": "S"}
    text = gemini_chunk([bare, bare, named], "STOP")
    items, error = await collect(GEMINI.serving(text), hello_walk())
    assert error is None
    calls = [i for i in items if isinstance(i, Part.Call)]
    assert len({c.id for c in calls}) == 3, "the made-up ids are unique"
    assert calls[2].id == "api-1"
    answers = [(c.id, Outcome.Ok(f"r{i}"), Author.Tools()) for i, c in enumerate(calls)]
    walk = walk_of(Body.User("go"), Body.Assistant(calls, Stop.Tools, None), Body.Results(answers))
    tools = [FakeTool("ta", "x"), FakeTool("tb", "x")]
    _, _, body = await sent_request(GEMINI, walk, GEMINI_BARE, tools)
    model_turn, answered = body["contents"][1:]  # type: ignore[index]
    assert model_turn["parts"] == [
        {"functionCall": {"name": "ta", "args": {"p": 1}}},
        {"functionCall": {"name": "ta", "args": {"p": 1}}},
        {"functionCall": {"name": "tb", "args": {}, "id": "api-1"}, "thoughtSignature": "S"},
    ], "an id goes back only where the API gave one"
    assert answered["parts"] == [
        {"functionResponse": {"name": "ta", "response": {"result": "r0"}}},
        {"functionResponse": {"name": "ta", "response": {"result": "r1"}}},
        {"functionResponse": {"name": "tb", "response": {"result": "r2"}, "id": "api-1"}},
    ]


@scenario
async def test_a_signature_ends_the_text_part_it_sits_on_and_each_part_goes_back_as_it_came() -> (
    None
):
    """A signature rides the last chunk of the text it signs; text after it is a new part."""
    chunks = [
        gemini_chunk([{"text": "one "}]),
        gemini_chunk([{"text": "two", "thoughtSignature": "SIG1"}]),
        gemini_chunk([{"text": "three"}]),
        gemini_chunk([{"text": "", "thoughtSignature": "SIG2"}]),
        gemini_chunk([{"text": "four"}], "STOP"),
    ]
    items, error = await collect(GEMINI.serving("".join(chunks)), hello_walk())
    assert error is None
    parts = [i for i in items if isinstance(i, Part)]
    assert parts == [
        Part.Text("one two", replay(thoughtSignature="SIG1")),
        Part.Text("three", replay(thoughtSignature="SIG2")),
        Part.Text("four"),
    ]
    walk = walk_of(Body.User("go"), Body.Assistant(parts, Stop.End, None), Body.User("next"))
    _, _, body = await sent_request(GEMINI, walk, GEMINI_BARE)
    assert body["contents"][1]["parts"] == [  # type: ignore[index]
        {"text": "one two", "thoughtSignature": "SIG1"},
        {"text": "three", "thoughtSignature": "SIG2"},
        {"text": "four"},
    ]


@pytest.mark.parametrize(
    ("reason", "stop"),
    [
        ("SAFETY", Stop.End),
        ("CONTINUATION", Stop.Length),
        ("ESCALATION", Stop.End),
    ],
)
@scenario
async def test_the_finish_reasons_beyond_stop_normalize_and_keep_their_text(
    reason: str, stop: Stop
) -> None:
    text = gemini_chunk([{"text": "x"}], reason)
    items, error = await collect(GEMINI.serving(text), hello_walk())
    assert error is None
    assert items == [Part.Text("x"), Finish(stop, reason)]


@scenario
async def test_a_finish_reason_the_provider_does_not_list_fails_after_the_parts() -> None:
    odd = gemini_chunk([{"text": "x"}], "MALFORMED_FUNCTION_CALL")
    items, error = await collect(GEMINI.serving(odd), hello_walk())
    assert error is not None
    assert "MALFORMED_FUNCTION_CALL" in str(error)
    assert [i for i in items if isinstance(i, Part)] == [Part.Text("x")], "what arrived is kept"


@scenario
async def test_a_blocked_prompt_is_refused_with_the_block_in_the_meta() -> None:
    block = {"promptFeedback": {"blockReason": "PROHIBITED_CONTENT"}}
    text = f"data: {json.dumps(block)}\r\n\r\n"
    items, error = await collect(GEMINI.serving(text), hello_walk())
    assert error is None
    finish = items[-1]
    assert isinstance(finish, Finish)
    assert (finish.stop, finish.reason) == (Stop.End, "PROHIBITED_CONTENT")
    assert finish.meta is not None
    assert json.loads(finish.meta)["promptFeedback"] == block["promptFeedback"]


@scenario
async def test_an_error_event_mid_stream_fails_with_its_code_after_the_parts_so_far() -> None:
    items, error = await collect_all(
        GEMINI.serving(recorded("gemini_error_midstream")), hello_walk()
    )
    assert [type(i) for i in items] == [ThinkingDelta, Part.Thinking]
    assert error is not None
    assert error.status == 503
    assert "high demand" in str(error)


@pytest.mark.parametrize(
    ("headers", "body", "wait"),
    [
        ({"retry-after": "7"}, {"error": {"code": 429, "message": "slow down"}}, 7.0),
        (
            {},
            {
                "error": {
                    "code": 429,
                    "message": "quota",
                    "status": "RESOURCE_EXHAUSTED",
                    "details": [
                        {
                            "@type": "type.googleapis.com/google.rpc.RetryInfo",
                            "retryDelay": "30s",
                        }
                    ],
                }
            },
            30.0,
        ),
    ],
)
@scenario
async def test_http_429_is_a_sample_error_with_status_and_the_wait(
    headers: dict[str, str], body: dict[str, object], wait: float
) -> None:
    server = Server(httpx.Response(429, headers=headers, json=body))
    _, error = await collect(GEMINI.over(server), hello_walk())
    assert error is not None
    assert (error.status, error.retry_after) == (429, wait)
    assert "429" in str(error)


@scenario
async def test_an_empty_chunk_of_the_other_kind_does_not_split_a_run() -> None:
    chunks = [
        gemini_chunk([{"text": "a", "thought": True}]),
        gemini_chunk([{"text": ""}]),
        gemini_chunk([{"text": "b", "thought": True}]),
        gemini_chunk([{"text": "answer"}], "STOP"),
    ]
    items, error = await collect(GEMINI.serving("".join(chunks)), hello_walk())
    assert error is None
    assert [i for i in items if isinstance(i, Part)] == [Part.Thinking("ab"), Part.Text("answer")]

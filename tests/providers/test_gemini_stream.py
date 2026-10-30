"""The Gemini provider's response handling: streams recorded from the live API (key never in
them; `fixtures/gemini_*.sse`) and ones written from the API reference's shapes."""

import asyncio
import json
from pathlib import Path

import httpx
import pytest
from gemini_support import (
    ENVIRON,
    FIXTURES,
    collect,
    gemini_chunk,
    gemini_fixture,
    gemini_walk,
    provider_for,
    running,
)
from harness import scenario
from transcripts import GatedStream, Server

from tau_next import (
    Body,
    CallArgs,
    CallNamed,
    Finish,
    Live,
    Part,
    Replay,
    Stop,
    TextDelta,
    ThinkingDelta,
)
from tau_next.providers.gemini import GeminiProvider


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
    items, error = await collect(gemini_fixture("gemini_text_thinking"), gemini_walk())
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
    items, _ = await collect(gemini_fixture("gemini_text_thinking"), gemini_walk())
    finish = items[-1]
    assert isinstance(finish, Finish)
    assert (finish.stop, finish.reason) == (Stop.Length, "MAX_TOKENS")
    assert finish.meta is not None
    meta = json.loads(finish.meta)
    assert meta["usage"] == {"input": 19, "output": 59 + 1437, "cached": 0}
    assert meta["usage_raw"]["thoughtsTokenCount"] == 1437
    assert (meta["model"], meta["id"]) == ("gemini-3.6-flash", meta["id"])


@scenario
async def test_cached_tokens_come_from_cachedContentTokenCount() -> None:
    usage = {"promptTokenCount": 100, "cachedContentTokenCount": 60, "candidatesTokenCount": 5}
    text = gemini_chunk([{"text": "x"}], "STOP", usage)
    items, _ = await collect(provider_for(text), gemini_walk())
    finish = items[-1]
    assert isinstance(finish, Finish)
    assert finish.meta is not None
    assert json.loads(finish.meta)["usage"] == {"input": 100, "output": 5, "cached": 60}


@scenario
async def test_a_call_arrives_whole_and_the_api_id_and_signature_are_kept() -> None:
    items, error = await collect(gemini_fixture("gemini_call"), gemini_walk())
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
async def test_a_call_with_no_id_is_given_a_unique_one_and_no_replay_id() -> None:
    call = {"functionCall": {"name": "ta", "args": {"p": 1}}}
    text = gemini_chunk([call, call], "STOP")
    items, error = await collect(provider_for(text), gemini_walk())
    assert error is None
    calls = [i for i in items if isinstance(i, Part.Call)]
    replay_marker = Replay("gemini", '{"synthesized_id": true}')
    ids = [c.id for c in calls]
    assert len(set(ids)) == 2
    assert all(i.startswith("gemini-") for i in ids)
    assert [c.replay for c in calls] == [replay_marker] * 2, "marked: never sent back as an id"
    assert [i.index for i in items if isinstance(i, CallNamed)] == [0, 1]


@scenario
async def test_a_signature_ends_the_text_part_it_sits_on() -> None:
    """A signature rides the last chunk of the text it signs; text after it is a new part."""
    chunks = [
        gemini_chunk([{"text": "one "}]),
        gemini_chunk([{"text": "two", "thoughtSignature": "SIG1"}]),
        gemini_chunk([{"text": "three"}]),
        gemini_chunk([{"text": "", "thoughtSignature": "SIG2"}], "STOP"),
    ]
    items, error = await collect(provider_for("".join(chunks)), gemini_walk())
    assert error is None
    assert [i for i in items if isinstance(i, Part)] == [
        Part.Text("one two", replay(thoughtSignature="SIG1")),
        Part.Text("three", replay(thoughtSignature="SIG2")),
    ]


@scenario
async def test_a_safety_finish_is_refused_and_an_unlisted_reason_fails() -> None:
    safety = gemini_chunk([{"text": "I can't"}], "SAFETY")
    items, error = await collect(provider_for(safety), gemini_walk())
    assert error is None
    assert [i for i in items if isinstance(i, Part)] == [Part.Text("I can't")]
    assert items[-1] == Finish(Stop.Refused, "SAFETY")
    odd = gemini_chunk([{"text": "x"}], "MALFORMED_FUNCTION_CALL")
    items, error = await collect(provider_for(odd), gemini_walk())
    assert error is not None
    assert "MALFORMED_FUNCTION_CALL" in str(error)
    assert [i for i in items if isinstance(i, Part)] == [Part.Text("x")], "what arrived is kept"


@scenario
async def test_a_blocked_prompt_is_refused_with_the_block_in_the_meta() -> None:
    block = {"promptFeedback": {"blockReason": "PROHIBITED_CONTENT"}}
    text = f"data: {json.dumps(block)}\r\n\r\n"
    items, error = await collect(provider_for(text), gemini_walk())
    assert error is None
    finish = items[-1]
    assert isinstance(finish, Finish)
    assert (finish.stop, finish.reason) == (Stop.Refused, "PROHIBITED_CONTENT")
    assert finish.meta is not None
    assert json.loads(finish.meta)["promptFeedback"] == block["promptFeedback"]


@scenario
async def test_an_error_event_mid_stream_fails_with_its_code_after_the_parts_so_far() -> None:
    items, error = await collect(gemini_fixture("gemini_error_midstream"), gemini_walk())
    assert [type(i) for i in items] == [ThinkingDelta, Part.Thinking]
    assert error is not None
    assert error.status == 503
    assert "high demand" in str(error)


@scenario
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
async def test_http_429_is_a_sample_error_with_status_and_the_wait(
    headers: dict[str, str], body: dict[str, object], wait: float
) -> None:
    server = Server(httpx.Response(429, headers=headers, json=body))
    _, error = await collect(GeminiProvider(server.transport, ENVIRON), gemini_walk())
    assert error is not None
    assert (error.status, error.retry_after) == (429, wait)
    assert str(error).startswith("HTTP 429: ")


@scenario
async def test_a_cancel_mid_text_leaves_an_interrupted_reply_holding_what_arrived(
    tmp_path: Path,
) -> None:
    gate = asyncio.Event()
    head = gemini_chunk([{"text": "hm", "thought": True}]) + gemini_chunk([{"text": "hel"}])
    head += gemini_chunk([{"text": "lo"}])
    response = httpx.Response(200, stream=GatedStream(head.encode(), gate, b""))
    provider = GeminiProvider(Server(response).transport, ENVIRON)
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
    assert reply == Body.Assistant(
        [Part.Thinking("hm"), Part.Text("hello")], Stop.Interrupted, None
    )


@scenario
@pytest.mark.parametrize(
    ("reason", "stop"), [("CONTINUATION", Stop.Length), ("ESCALATION", Stop.Refused)]
)
async def test_continuation_is_length_and_escalation_is_refused(reason: str, stop: Stop) -> None:
    text = gemini_chunk([{"text": "x"}], reason)
    items, error = await collect(provider_for(text), gemini_walk())
    assert error is None
    assert items[-1] == Finish(stop, reason)


@scenario
async def test_an_empty_chunk_of_the_other_kind_does_not_split_a_run() -> None:
    chunks = [
        gemini_chunk([{"text": "a", "thought": True}]),
        gemini_chunk([{"text": ""}]),
        gemini_chunk([{"text": "b", "thought": True}]),
        gemini_chunk([{"text": "answer"}], "STOP"),
    ]
    items, error = await collect(provider_for("".join(chunks)), gemini_walk())
    assert error is None
    assert [i for i in items if isinstance(i, Part)] == [Part.Thinking("ab"), Part.Text("answer")]

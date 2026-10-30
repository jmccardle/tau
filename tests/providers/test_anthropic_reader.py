"""The Anthropic reader's half of the `ReplyReader` contract, and replay it cannot understand."""

import json

import httpx
from anthropic_streams import (
    HEADERS,
    closed,
    collect,
    delta,
    event,
    message,
    opened,
    provider,
    sent_request,
    start,
    stream,
    text,
)
from gemini_support import walk_of
from harness import scenario
from transcripts import CutStream, Server, fresh_walk

from tau_next import Body, Finish, Part, Replay, Stop


@scenario
async def test_a_stop_for_another_block_is_refused_not_taken_for_the_open_one() -> None:
    body = start() + opened(0, {"type": "text", "text": ""}) + closed(7)
    items, error = await collect(provider(stream(body)), fresh_walk())
    assert error is not None
    assert "block 7" in str(error)
    assert items == []


@scenario
async def test_a_null_count_in_a_later_usage_does_not_wipe_the_normalized_usage() -> None:
    usage = {"output_tokens": 15, "input_tokens": None}
    items, error = await collect(
        provider(stream(message(text(0, ["hi"]), usage=usage))), fresh_walk()
    )
    assert error is None
    assert isinstance(items[-1], Finish)
    assert items[-1].meta is not None
    assert json.loads(items[-1].meta)["usage"] == {"input": 25, "output": 15, "cached": 0}


@scenario
async def test_an_error_event_with_no_error_object_is_a_500_that_keeps_the_value() -> None:
    body = start() + event("error", error="overloaded, go away")
    _, error = await collect(provider(stream(body)), fresh_walk())
    assert error is not None
    assert error.status == 500
    assert "overloaded, go away" in str(error)


@scenario
async def test_a_call_cut_off_is_dropped_and_its_text_goes_in_the_error() -> None:
    head = (
        start()
        + opened(0, {"type": "tool_use", "id": "toolu_1", "name": "ta", "input": {}})
        + delta(0, type="input_json_delta", partial_json='{"p"')
    )
    server = Server(httpx.Response(200, headers=HEADERS, stream=CutStream(head.encode())))
    items, error = await collect(provider(server), fresh_walk())
    assert items == []
    assert error is not None
    assert "a call was cut off and dropped" in str(error)
    assert '{"p"' in str(error)


@scenario
async def test_replay_of_a_type_this_version_does_not_know_counts_as_unsigned() -> None:
    odd = Replay("anthropic", json.dumps({"type": "future_thinking", "x": 1}))
    walk = walk_of(
        Body.User("q"),
        Body.Assistant([Part.Thinking("t", odd), Part.Text("a")], Stop.End, None),
        Body.User("q2"),
    )
    body = (await sent_request(walk))[2]
    contents = [m["content"] for m in body["messages"]]  # type: ignore[attr-defined]
    assert all(block["type"] != "thinking" for c in contents for block in c)

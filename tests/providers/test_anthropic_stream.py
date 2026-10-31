"""The Anthropic provider's response side: events to parts and deltas, replay data, stops,
usage, errors, and the one long-lived client. Streams are written by hand (anthropic_streams)."""

import json
from pathlib import Path

import httpx
import pytest
from fakes import FakeTool
from harness import scenario
from wire import (
    ANTHROPIC,
    ANTHROPIC_MODEL,
    SIGNATURE,
    THINKING,
    Server,
    bodies,
    call,
    collect,
    collect_all,
    delta,
    event,
    finish,
    fresh_walk,
    message,
    opened,
    redacted,
    running,
    sse_texts,
    start,
    text,
    thinking,
)

from tau_next import (
    Body,
    CallArgs,
    CallNamed,
    Finish,
    Part,
    Replay,
    Stop,
    TextDelta,
    ThinkingDelta,
)

HEADERS = {"content-type": "text/event-stream"}

SIGNED = Part.Thinking(
    THINKING, Replay("anthropic", json.dumps({"type": "thinking", "signature": SIGNATURE}))
)


def provider(server: Server):
    return ANTHROPIC.over(server)


def stream(*messages: str) -> Server:
    """A server that streams each of `messages` for each request in turn, the last repeated."""
    return sse_texts(*messages)


@scenario
async def test_thinking_streams_as_deltas_and_ends_as_a_signed_part_before_the_text() -> None:
    chunks = [THINKING[:10], THINKING[10:]]
    server = stream(message(thinking(0, chunks), text(1, ["Hel", "lo"])))
    items, error = await collect_all(provider(server), fresh_walk())
    assert error is None
    assert items[:-1] == [
        ThinkingDelta(chunks[0]),
        ThinkingDelta(chunks[1]),
        SIGNED,
        TextDelta("Hel"),
        TextDelta("lo"),
        Part.Text("Hello"),
    ]
    last = items[-1]
    assert isinstance(last, Finish)
    assert (last.stop, last.reason) == (Stop.End, "end_turn")


@scenario
async def test_a_call_is_named_then_its_arguments_stream_and_no_arguments_are_braces() -> None:
    server = stream(
        message(
            call(0, "toolu_01A", "get_weather", ["", '{"city":', ' "Paris"}']),
            call(1, "toolu_01B", "ping", [""]),
            reason="tool_use",
        )
    )
    items, error = await collect_all(provider(server), fresh_walk())
    assert error is None
    assert items[:-1] == [
        CallNamed(0, "toolu_01A", "get_weather"),
        CallArgs(0, '{"city":'),
        CallArgs(0, ' "Paris"}'),
        Part.Call("toolu_01A", "get_weather", '{"city": "Paris"}'),
        CallNamed(1, "toolu_01B", "ping"),
        CallArgs(1, "{}"),
        Part.Call("toolu_01B", "ping", "{}"),
    ]
    assert isinstance(items[-1], Finish)
    assert items[-1].stop == Stop.Tools


@scenario
async def test_redacted_thinking_is_an_empty_thinking_part_holding_its_data() -> None:
    server = stream(message(redacted(0, "EmwKAhgBEgy3va3pzix/LafPsn4a"), text(1, ["ok"])))
    items, _ = await collect(provider(server), fresh_walk())
    assert items[0] == Part.Thinking(
        "",
        Replay(
            "anthropic",
            json.dumps({"type": "redacted_thinking", "data": "EmwKAhgBEgy3va3pzix/LafPsn4a"}),
        ),
    )


@scenario
async def test_omitted_thinking_keeps_its_signature_on_an_empty_part() -> None:
    """`display: omitted` streams an empty `thinking_delta` and then the signature."""
    server = stream(message(thinking(0, [""]), text(1, ["ok"])))
    items, _ = await collect_all(provider(server), fresh_walk())
    assert items[0] == Part.Thinking(
        "", Replay("anthropic", json.dumps({"type": "thinking", "signature": SIGNATURE}))
    )


@scenario
@pytest.mark.parametrize(
    ("reason", "stop"),
    [
        ("end_turn", Stop.End),
        ("stop_sequence", Stop.End),
        ("tool_use", Stop.Tools),
        ("max_tokens", Stop.Length),
        ("model_context_window_exceeded", Stop.Length),
        ("pause_turn", Stop.Length),
        ("refusal", Stop.End),
    ],
)
async def test_each_stop_reason_the_docs_list_maps_and_keeps_its_own_string(
    reason: str, stop: Stop
) -> None:
    server = stream(message(text(0, ["x"]), reason=reason))
    items, error = await collect(provider(server), fresh_walk())
    assert error is None
    assert isinstance(items[-1], Finish)
    assert (items[-1].stop, items[-1].reason) == (stop, reason)


@scenario
async def test_an_unknown_stop_reason_fails_the_sample_after_the_parts() -> None:
    server = stream(message(text(0, ["x"]), reason="brand_new"))
    items, error = await collect(provider(server), fresh_walk())
    assert items == [Part.Text("x")]
    assert error is not None
    assert "brand_new" in str(error)


@scenario
async def test_usage_is_normalized_with_cache_writes_counted_as_input_and_reads_as_cached() -> None:
    first = {
        "input_tokens": 12,
        "cache_creation_input_tokens": 100,
        "cache_read_input_tokens": 2000,
        "output_tokens": 1,
    }
    body = start(first) + text(0, ["x"]) + finish("end_turn", {"output_tokens": 77})
    server = stream(body + event("message_stop"))
    items, _ = await collect(provider(server), fresh_walk())
    finished = items[-1]
    assert isinstance(finished, Finish)
    assert finished.meta is not None
    meta = json.loads(finished.meta)
    assert meta["usage"] == {"input": 2112, "output": 77, "cached": 2000}
    assert meta["usage_raw"] == {**first, "output_tokens": 77}
    assert (meta["id"], meta["model"]) == ("msg_01XFDUDYJgAACzvnptvVoYEL", ANTHROPIC_MODEL)


@pytest.mark.parametrize(
    ("kind", "status"), [("overloaded_error", 529), ("conflict_error", 409), ("api_error", 500)]
)
@scenario
async def test_a_stream_error_event_is_a_sample_error_with_its_status(
    kind: str, status: int
) -> None:
    body = start() + opened(0, {"type": "text", "text": ""})
    body += delta(0, type="text_delta", text="par")
    body += event("error", error={"type": kind, "message": "Busy"})
    items, error = await collect(provider(stream(body)), fresh_walk())
    assert items == [Part.Text("par")], "what had arrived is kept"
    assert error is not None
    assert (error.status, error.retry_after) == (status, None)
    assert "Busy" in str(error)


@scenario
async def test_an_http_429_carries_its_status_retry_after_and_the_api_message() -> None:
    body = {"type": "error", "error": {"type": "rate_limit_error", "message": "slow down"}}
    server = Server(httpx.Response(429, headers={"retry-after": "7"}, json=body))
    _, error = await collect(provider(server), fresh_walk())
    assert error is not None
    assert (error.status, error.retry_after) == (429, 7.0)
    assert "rate_limit_error: slow down" in str(error)


@scenario
async def test_an_error_after_the_stop_reason_costs_only_the_usage_still_to_come() -> None:
    body = (
        start()
        + text(0, ["x"])
        + finish("end_turn")
        + event("error", error={"type": "api_error", "message": "late"})
    )
    items, error = await collect(provider(stream(body)), fresh_walk())
    assert error is None
    assert items[0] == Part.Text("x")
    assert isinstance(items[-1], Finish)


@scenario
async def test_a_stream_that_ends_without_a_stop_reason_fails() -> None:
    body = start() + text(0, ["x"])
    items, error = await collect(provider(stream(body)), fresh_walk())
    assert items == [Part.Text("x")]
    assert error is not None
    assert "stop_reason" in str(error)


@scenario
async def test_an_unreadable_block_fails_loudly_and_unknown_events_are_skipped() -> None:
    odd = opened(
        0, {"type": "server_tool_use", "id": "srvtoolu_1", "name": "web_search", "input": {}}
    )
    _, error = await collect(provider(stream(message(odd))), fresh_walk())
    assert error is not None
    assert "server_tool_use" in str(error)
    fine = message(event("brand_new_event", x=1), text(0, ["ok"]))
    items, error = await collect(provider(stream(fine)), fresh_walk())
    assert error is None
    assert items[0] == Part.Text("ok")


# Under the driver


@scenario
async def test_a_tool_loop_sends_the_signed_thinking_the_tool_use_and_each_result(
    tmp_path: Path,
) -> None:
    first = message(
        thinking(0, [THINKING]),
        call(1, "toolu_01A", "ta", ["", '{"x": ', "1}"]),
        call(2, "toolu_01B", "tb", [""]),
        reason="tool_use",
    )
    server = stream(first, message(text(0, ["done"])))
    tools = [FakeTool("ta", "A"), FakeTool("tb", RuntimeError("boom"))]
    async with running(tmp_path / "s", provider(server), tools=tools) as (_, driver, cursor):
        await driver.client("test").send(cursor, "go")
        await driver.rest(cursor)
    second = server.bodies[1]
    assert second["messages"][1:] == [  # type: ignore[index]
        {
            "role": "assistant",
            "content": [
                {"type": "thinking", "thinking": THINKING, "signature": SIGNATURE},
                {"type": "tool_use", "id": "toolu_01A", "name": "ta", "input": {"x": 1}},
                {"type": "tool_use", "id": "toolu_01B", "name": "tb", "input": {}},
            ],
        },
        {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "toolu_01A",
                    "content": "A",
                    "is_error": False,
                },
                {
                    "type": "tool_result",
                    "tool_use_id": "toolu_01B",
                    "content": "Error: RuntimeError: boom",
                    "is_error": True,
                    "cache_control": {"type": "ephemeral"},
                },
            ],
        },
    ]
    assert [t["name"] for t in server.bodies[0]["tools"]] == ["ta", "tb"]  # type: ignore[index]
    assert second["tools"][0]["input_schema"] == {"type": "object", "properties": {}}  # type: ignore[index]


@scenario
async def test_signed_thinking_comes_back_byte_identical_on_the_next_request(
    tmp_path: Path,
) -> None:
    server = stream(
        message(thinking(0, [THINKING]), text(1, ["Hello"])), message(text(0, ["again"]))
    )
    async with running(tmp_path / "s", provider(server)) as (session, driver, cursor):
        await driver.client("test").send(cursor, "hi")
        await driver.rest(cursor)
        saved = bodies(session, cursor)[-1]
        assert isinstance(saved, Body.Assistant)
        assert saved.parts[0] == SIGNED
        await driver.client("test").send(cursor, "more")
        await driver.rest(cursor)
    sent = server.bodies[1]["messages"]
    assert sent[1] == {  # type: ignore[index]
        "role": "assistant",
        "content": [
            {"type": "thinking", "thinking": THINKING, "signature": SIGNATURE},
            {"type": "text", "text": "Hello"},
        ],
    }
    wire = server.requests[1].content.decode()
    assert json.dumps(SIGNATURE) in wire
    assert json.dumps(THINKING) in wire or json.dumps(THINKING, ensure_ascii=False) in wire


@scenario
async def test_redacted_thinking_comes_back_on_the_next_request(tmp_path: Path) -> None:
    server = stream(
        message(redacted(0, "EmwKAhgBEgy3va3pzix/LafPsn4a"), text(1, ["Hello"])),
        message(text(0, ["x"])),
    )
    async with running(tmp_path / "s", provider(server)) as (_, driver, cursor):
        await driver.client("test").send(cursor, "hi")
        await driver.rest(cursor)
        await driver.client("test").send(cursor, "more")
        await driver.rest(cursor)
    sent = server.bodies[1]["messages"]
    assert sent[1]["content"][0] == {  # type: ignore[index]
        "type": "redacted_thinking",
        "data": "EmwKAhgBEgy3va3pzix/LafPsn4a",
    }


@scenario
async def test_the_request_id_header_is_kept_in_meta_on_success_and_on_failure() -> None:
    headers = {**HEADERS, "request-id": "req_018EeWyXxfu5pfWkrYcMdjWG"}
    ok = Server(httpx.Response(200, headers=headers, text=message(text(0, ["x"]))))
    items, _ = await collect(provider(ok), fresh_walk())
    assert isinstance(items[-1], Finish)
    assert items[-1].meta is not None
    assert json.loads(items[-1].meta)["request_id"] == "req_018EeWyXxfu5pfWkrYcMdjWG"
    bad = Server(httpx.Response(500, headers=headers, json={"type": "error", "error": {}}))
    _, error = await collect(provider(bad), fresh_walk())
    assert error is not None
    assert error.meta is not None
    assert json.loads(error.meta) == {"request_id": "req_018EeWyXxfu5pfWkrYcMdjWG"}

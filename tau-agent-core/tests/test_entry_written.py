"""The ``entry_written`` hook and its write groups (docs/STEP-DEBUGGER.md §3.2).

``entry_written`` fires once per message entry, in order, and only after the
last write of the entry's group has returned, so a handler (later, a
suspension) never runs while a sibling entry is held unwritten.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime
from typing import Any
from unittest.mock import patch

import pytest

from tau_llm.streaming import DoneEvent, ErrorEvent, TextDeltaEvent
from tau_llm.tools import ToolDefinition
from tau_llm.types import AssistantMessage, Model, TextContent, ToolCall, Usage

from tau_agent_core.agent_session import AgentSession
from tau_agent_core.session_log import InMemorySessionLog
from tau_agent_core.submission import Submission
from tau_agent_core.tools.base import AgentTool

_TS = 1_700_000_000_000


class _Stream:
    def __init__(self, events: list[Any]) -> None:
        self._events = events

    def __aiter__(self) -> "_Stream":
        self._i = 0
        return self

    async def __anext__(self) -> Any:
        if self._i >= len(self._events):
            raise StopAsyncIteration
        event = self._events[self._i]
        self._i += 1
        return event


def _assistant(content: list[Any], stop_reason: str = "stop") -> AssistantMessage:
    return AssistantMessage(
        content=content,
        api="openai-completions",
        provider="openai",
        model="gpt-4o",
        stop_reason=stop_reason,  # type: ignore[arg-type]
        timestamp=_TS,
        usage=Usage(),
    )


def _text(text: str) -> AssistantMessage:
    return _assistant([TextContent(text=text)])


def _named_calls(*names: str) -> AssistantMessage:
    return _assistant(
        [ToolCall(id=f"c-{n}", name=n, arguments={}) for n in names], stop_reason="toolUse"
    )


def _script(*finals: AssistantMessage):
    queue = list(finals)

    async def fake(model, context, options=None):
        return _Stream([DoneEvent(final=queue.pop(0), usage=Usage())])

    return fake


def _model() -> Model:
    return Model(
        id="gpt-4o",
        name="GPT-4o",
        api="openai-completions",
        provider="openai",
        base_url="https://api.openai.com/v1",
        context_window=128000,
        max_tokens=4096,
    )


def _session(*extensions: Any, **kwargs: Any) -> AgentSession:
    return AgentSession(
        session_log=InMemorySessionLog(), model=_model(), extensions=list(extensions), **kwargs
    )


def _tool(name: str, delay: float = 0.0) -> AgentTool:
    async def _execute(**kw: Any) -> str:
        await asyncio.sleep(delay)
        return f"{name} ok"

    return AgentTool(
        definition=ToolDefinition(
            name=name,
            label=name,
            description=f"the {name} tool",
            parameters={"type": "object", "properties": {}, "required": []},
            execute=_execute,
        )
    )


def _message_entries(session: AgentSession) -> list[dict[str, Any]]:
    return [
        e
        for e in session.session_log.entries()
        if e.get("type") in ("message", "customMessage") and e["message"].get("role") != "system"
    ]


class _Recorder:
    """An extension recording each ``entry_written`` with the ids the log held at that moment."""

    def __init__(self) -> None:
        self.session: AgentSession | None = None
        self.seen: list[dict[str, Any]] = []

    def __call__(self, api: Any) -> None:
        def on_written(event: dict[str, Any], ctx: Any) -> None:
            assert self.session is not None
            self.seen.append(
                {
                    "kind": event["kind"],
                    "entry_id": event["entry_id"],
                    "message": event["message"],
                    "blocks": event["blocks"],
                    "logged": [e["id"] for e in _message_entries(self.session)],
                }
            )

        api.on("entry_written", on_written)

    def bind(self, session: AgentSession) -> AgentSession:
        self.session = session
        return session

    def text_of(self, i: int) -> str:
        return self.seen[i]["blocks"][0]["text"]

    def logged_when(self, entry_id: str) -> list[str]:
        return next(s["logged"] for s in self.seen if s["entry_id"] == entry_id)


def _id_of(session: AgentSession, predicate: Any) -> str:
    return next(e["id"] for e in _message_entries(session) if predicate(e["message"]))


async def test_fires_once_per_message_entry_in_write_order() -> None:
    """Prompt, assistant, tool result, turn_end node, steer, user_turn_end node, send_message."""
    rec = _Recorder()
    holder: dict[str, AgentSession] = {}

    def nodes(api: Any) -> None:
        api.on(
            "turn_end",
            lambda e, ctx: (
                {"message": {"customType": "note", "content": "t0"}}
                if e["turn_index"] == 0
                else None
            ),
        )
        api.on("user_turn_end", lambda e, ctx: {"message": {"customType": "done", "content": "u"}})

    finals = [_named_calls("probe"), _text("fin")]

    async def fake(model, context, options=None):
        final = finals.pop(0)
        if finals:
            holder["s"].cursor.steer_queue.append(
                {"role": "user", "content": [{"type": "text", "text": "steer!"}], "timestamp": _TS}
            )
        return _Stream([DoneEvent(final=final, usage=Usage())])

    session = rec.bind(_session(rec, nodes, tools=[_tool("probe")]))
    holder["s"] = session
    with patch("tau_agent_core.agent_loop.stream_simple", side_effect=fake):
        await session.prompt("go")
    await session._append_custom_message({"customType": "aside", "content": "later"})

    written = _message_entries(session)
    assert [s["entry_id"] for s in rec.seen] == [e["id"] for e in written]
    assert [s["kind"] for s in rec.seen] == [
        "user",
        "assistant",
        "toolResult",
        "custom",
        "user",
        "assistant",
        "custom",
        "custom",
    ]
    assert [s["message"] for s in rec.seen] == [e["message"] for e in written]
    assert all(s["entry_id"] in s["logged"] for s in rec.seen)


async def test_the_prompt_batch_is_one_group() -> None:
    rec = _Recorder()

    def injector(api: Any) -> None:
        api.on(
            "before_agent_start",
            lambda e, ctx: {
                "message": {"customType": "pre", "content": "before", "position": "before_user"}
            },
        )

    def injector2(api: Any) -> None:
        api.on(
            "before_agent_start",
            lambda e, ctx: {"message": {"customType": "post", "content": "after"}},
        )

    session = rec.bind(_session(rec, injector, injector2))
    with patch("tau_agent_core.agent_loop.stream_simple", side_effect=_script(_text("ok"))):
        await session.prompt("go")

    batch = [s for s in rec.seen if s["kind"] != "assistant"]
    assert [s["blocks"][0]["text"] for s in batch] == ["before", "go", "after"]
    after_id = batch[2]["entry_id"]
    assert after_id in batch[0]["logged"], "the first entry fired before the batch's last write"


async def test_one_steer_drain_is_one_group() -> None:
    rec = _Recorder()
    holder: dict[str, AgentSession] = {}
    finals = [_named_calls("probe"), _text("fin")]

    async def fake(model, context, options=None):
        final = finals.pop(0)
        if finals:
            for text in ("s1", "s2"):
                holder["s"].cursor.steer_queue.append(
                    {"role": "user", "content": [{"type": "text", "text": text}], "timestamp": _TS}
                )
        return _Stream([DoneEvent(final=final, usage=Usage())])

    session = rec.bind(_session(rec, tools=[_tool("probe")]))
    holder["s"] = session
    with patch("tau_agent_core.agent_loop.stream_simple", side_effect=fake):
        await session.prompt("go")

    steers = [s for s in rec.seen if s["kind"] == "user" and s["blocks"][0]["text"] != "go"]
    assert [s["blocks"][0]["text"] for s in steers] == ["s1", "s2"]
    assert steers[1]["entry_id"] in steers[0]["logged"]


@pytest.mark.parametrize("hook", ["turn_end", "user_turn_end"])
async def test_the_nodes_one_dispatch_injects_are_one_group(hook: str) -> None:
    rec = _Recorder()

    def one(api: Any) -> None:
        api.on(hook, lambda e, ctx: {"message": {"customType": "a", "content": "n1"}})

    def two(api: Any) -> None:
        api.on(hook, lambda e, ctx: {"message": {"customType": "b", "content": "n2"}})

    session = rec.bind(_session(rec, one, two))
    with patch("tau_agent_core.agent_loop.stream_simple", side_effect=_script(_text("ok"))):
        await session.prompt("go")

    nodes = [s for s in rec.seen if s["kind"] == "custom"]
    assert [s["blocks"][0]["text"] for s in nodes] == ["n1", "n2"]
    assert nodes[1]["entry_id"] in nodes[0]["logged"]


async def test_a_parallel_batch_fires_only_after_every_result_is_written() -> None:
    rec = _Recorder()
    session = rec.bind(_session(rec, tools=[_tool("fast"), _tool("slow", delay=0.05)]))
    with patch(
        "tau_agent_core.agent_loop.stream_simple",
        side_effect=_script(_named_calls("fast", "slow"), _text("done")),
    ):
        await session.prompt("go")

    results = [s for s in rec.seen if s["kind"] == "toolResult"]
    assert [s["message"]["tool_name"] for s in results] == ["fast", "slow"]
    slow_id = results[1]["entry_id"]
    assert slow_id in results[0]["logged"], "fast's entry_written ran while slow was unwritten"


async def test_sequential_results_are_each_their_own_group() -> None:
    rec = _Recorder()
    session = rec.bind(
        _session(rec, tools=[_tool("first"), _tool("second")], tool_execution_mode="sequential")
    )
    with patch(
        "tau_agent_core.agent_loop.stream_simple",
        side_effect=_script(_named_calls("first", "second"), _text("done")),
    ):
        await session.prompt("go")

    results = [s for s in rec.seen if s["kind"] == "toolResult"]
    assert [s["message"]["tool_name"] for s in results] == ["first", "second"]
    assert results[1]["entry_id"] not in results[0]["logged"]


async def test_the_assistant_fires_before_its_tools_run() -> None:
    rec = _Recorder()
    ran: list[int] = []

    async def _execute(**kw: Any) -> str:
        ran.append(len(rec.seen))
        return "ok"

    tool = AgentTool(
        definition=ToolDefinition(
            name="probe",
            label="probe",
            description="probe",
            parameters={"type": "object", "properties": {}, "required": []},
            execute=_execute,
        )
    )
    session = rec.bind(_session(rec, tools=[tool]))
    with patch(
        "tau_agent_core.agent_loop.stream_simple",
        side_effect=_script(_named_calls("probe"), _text("done")),
    ):
        await session.prompt("go")
    assert [s["kind"] for s in rec.seen[: ran[0]]] == ["user", "assistant"]


async def test_a_stream_failure_is_one_group_and_the_raise_follows_it() -> None:
    rec = _Recorder()
    partial = _assistant(
        [
            ToolCall(id="c1", name="probe", arguments={}),
            ToolCall(id="c2", name="probe", arguments={}),
        ],
        stop_reason="toolUse",
    )

    async def fake(model, context, options=None):
        return _Stream(
            [TextDeltaEvent(delta="", partial=partial), ErrorEvent(message="upstream 503")]
        )

    session = rec.bind(_session(rec, tools=[_tool("probe")]))
    with patch("tau_agent_core.agent_loop.stream_simple", side_effect=fake):
        with pytest.raises(Exception, match="upstream 503"):
            await session.prompt("go")

    group = [s for s in rec.seen if s["kind"] in ("assistant", "toolResult")]
    assert [s["kind"] for s in group] == ["assistant", "toolResult", "toolResult"]
    assert group[0]["message"]["stop_reason"] == "error"
    assert group[2]["entry_id"] in group[0]["logged"]


async def test_not_dispatched_with_store_history_off() -> None:
    rec = _Recorder()
    session = rec.bind(_session(rec))
    sub = Submission(
        text="go", source="interactive", submitter="human", submission_id="s1", store_history=False
    )
    with patch("tau_agent_core.agent_loop.stream_simple", side_effect=_script(_text("x"))):
        result = await session.submit(sub)
    assert result.accepted
    assert rec.seen == []


async def test_a_handler_cannot_change_the_entry_or_what_a_later_handler_sees() -> None:
    later: list[str] = []

    def tamper(api: Any) -> None:
        def on_written(event: dict[str, Any], ctx: Any) -> None:
            for block in event["message"]["content"]:
                if block.get("type") == "text":
                    block["text"] = "TAMPERED"
            event["blocks"][0]["text"] = "TAMPERED"
            event["entry_id"] = "TAMPERED"

        api.on("entry_written", on_written)

    def observe(api: Any) -> None:
        api.on(
            "entry_written",
            lambda e, ctx: later.append(f"{e['entry_id'] != 'TAMPERED'}:{e['blocks'][0]['text']}"),
        )

    session = _session(tamper, observe)
    with patch("tau_agent_core.agent_loop.stream_simple", side_effect=_script(_text("x"))):
        await session.prompt("go")
    assert "TAMPERED" not in json.dumps(session.session_log.entries())
    assert later == ["True:go", "True:x"]


async def test_a_handler_error_is_surfaced_and_dispatch_continues() -> None:
    errors: list[Any] = []
    after: list[str] = []

    def failing(api: Any) -> None:
        def boom(event: dict[str, Any], ctx: Any) -> None:
            raise RuntimeError("entry_written boom")

        api.on("entry_written", boom)

    def observe(api: Any) -> None:
        api.on("entry_written", lambda e, ctx: after.append(e["kind"]))

    session = _session(failing, observe)
    session._extension_runner.on_error(errors.append)
    with patch("tau_agent_core.agent_loop.stream_simple", side_effect=_script(_text("x"))):
        messages = await session.prompt("go")
    assert [m["role"] for m in messages] == ["user", "assistant"]
    assert after == ["user", "assistant"]
    assert [(e.event, e.error) for e in errors] == [("entry_written", "entry_written boom")] * 2


async def test_underivable_blocks_skip_the_dispatch_and_the_batch_completes() -> None:
    """Only an ``entry_written`` observer loaded, and a result whose details are not JSON (#91)."""
    errors: list[Any] = []
    seen: list[str] = []

    async def _execute(**kw: Any) -> dict[str, Any]:
        return {
            "content": [{"type": "text", "text": "dated"}],
            "details": {"when": datetime(2026, 10, 24)},
        }

    dated = AgentTool(
        definition=ToolDefinition(
            name="dated",
            label="dated",
            description="dated",
            parameters={"type": "object", "properties": {}, "required": []},
            execute=_execute,
        )
    )

    def observer(api: Any) -> None:
        api.on("entry_written", lambda e, ctx: seen.append(e["kind"]))

    session = _session(observer, tools=[dated, _tool("plain")], tool_execution_mode="sequential")
    session._extension_runner.on_error(errors.append)
    calls = _assistant(
        [
            ToolCall(id="c1", name="dated", arguments={}),
            ToolCall(id="c2", name="plain", arguments={}),
        ],
        stop_reason="toolUse",
    )
    with patch(
        "tau_agent_core.agent_loop.stream_simple", side_effect=_script(calls, _text("done"))
    ):
        messages = await session.prompt("go")

    results = [m for m in messages if m["role"] == "toolResult"]
    assert [(r["tool_call_id"], r["content"][0]["text"]) for r in results] == [
        ("c1", "dated"),
        ("c2", "plain ok"),
    ]
    assert messages[-1]["role"] == "assistant"
    assert seen == ["user", "assistant", "toolResult", "assistant"]
    (error,) = errors
    assert error.event == "entry_written"
    assert "could not be derived" in error.error and "TypeError" in error.error

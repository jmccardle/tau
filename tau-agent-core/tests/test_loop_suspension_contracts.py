"""The loop contracts a suspension relies on (docs/STEP-DEBUGGER.md §3.2.2, §3.6, §3.9).

Nothing raises ``TurnSuspended`` yet; these tests raise it from a hook to pin
what the loop, ``submit_threadsafe`` and the runner do with it, plus the
``tool_call`` post-hook abort check and the ``input`` hook's ``refuse``.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import patch

import pytest

from tau_llm.streaming import DoneEvent
from tau_llm.tools import ToolDefinition
from tau_llm.types import AssistantMessage, Model, TextContent, ToolCall, Usage

from tau_agent_core.agent_loop import ABORTED_TOOL_RESULT
from tau_agent_core.agent_session import AgentSession
from tau_agent_core.session_log import InMemorySessionLog
from tau_agent_core.submission import Submission
from tau_agent_core.suspension import (
    SuspensionSwallowedError,
    TurnSuspended,
    mark_suspension_raised,
)
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


def _counting_tool(name: str, ran: list[str]) -> AgentTool:
    async def _execute(**kw: Any) -> str:
        ran.append(name)
        return f"{name} ok"

    return AgentTool(
        definition=ToolDefinition(
            name=name,
            label=name,
            description=name,
            parameters={"type": "object", "properties": {}, "required": []},
            execute=_execute,
        )
    )


def _entries_of(session: AgentSession, *roles: str) -> list[dict[str, Any]]:
    return [
        e
        for e in session.session_log.entries()
        if e.get("type") in ("message", "customMessage") and e["message"].get("role") in roles
    ]


def _agent_ends(api_events: list[Any]):
    def ext(api: Any) -> None:
        api.on("agent_end", lambda e: api_events.append(e))

    return ext


# ── the tool_call post-hook abort check (§3.6) ───────────────────────────────


@pytest.mark.parametrize("mode", ["sequential", "parallel"])
async def test_an_abort_tripped_in_a_tool_call_hook_runs_no_tool(mode: str) -> None:
    ran: list[str] = []
    holder: dict[str, AgentSession] = {}

    def tripper(api: Any) -> None:
        def on_call(event: dict[str, Any], ctx: Any) -> None:
            if event["tool_name"] == "first":
                holder["s"].cursor.abort_signal.abort(reason="user")

        api.on("tool_call", on_call)

    session = _session(
        tripper,
        tools=[_counting_tool("first", ran), _counting_tool("second", ran)],
        tool_execution_mode=mode,
    )
    holder["s"] = session
    with patch(
        "tau_agent_core.agent_loop.stream_simple",
        side_effect=_script(_named_calls("first", "second")),
    ):
        await session.prompt("go")

    assert ran == []
    results = _entries_of(session, "toolResult")
    assert [
        (r["message"]["tool_call_id"], r["message"]["content"][0]["text"]) for r in results
    ] == [
        ("c-first", ABORTED_TOOL_RESULT),
        ("c-second", ABORTED_TOOL_RESULT),
    ]


# ── TurnSuspended reaches the loop (§3.2.2) ──────────────────────────────────


async def test_turn_suspended_from_a_tool_call_hook_ends_the_turn_suspended() -> None:
    ends: list[Any] = []
    ran: list[str] = []

    def suspender(api: Any) -> None:
        def on_call(event: dict[str, Any], ctx: Any) -> None:
            raise TurnSuspended()

        api.on("tool_call", on_call)

    session = _session(suspender, _agent_ends(ends), tools=[_counting_tool("probe", ran)])
    with patch(
        "tau_agent_core.agent_loop.stream_simple", side_effect=_script(_named_calls("probe"))
    ):
        with pytest.raises(TurnSuspended):
            await session.prompt("go")

    assert ran == []
    assert [(e.end_reason, e.is_error, e.error) for e in ends] == [("suspended", False, None)]
    assert _entries_of(session, "toolResult") == []
    assert [e["message"]["role"] for e in _entries_of(session, "user", "assistant", "custom")] == [
        "user",
        "assistant",
    ]


async def test_turn_suspended_from_turn_end_in_run_continue_ends_the_turn_suspended() -> None:
    ends: list[Any] = []
    armed = {"on": False}

    def suspender(api: Any) -> None:
        def on_turn_end(event: dict[str, Any], ctx: Any) -> None:
            if armed["on"]:
                raise TurnSuspended()

        api.on("turn_end", on_turn_end)

    session = _session(suspender, _agent_ends(ends))
    with patch(
        "tau_agent_core.agent_loop.stream_simple", side_effect=_script(_text("one"), _text("two"))
    ):
        await session.prompt("go")
        armed["on"] = True
        with pytest.raises(TurnSuspended):
            await session.continue_conversation()

    assert [e.end_reason for e in ends] == ["done", "suspended"]
    assert all(
        e["message"].get("customType") != "turn_error" for e in _entries_of(session, "custom")
    )


async def test_submit_threadsafe_resolves_a_suspended_turn_as_accepted() -> None:
    def suspender(api: Any) -> None:
        def on_turn_end(event: dict[str, Any], ctx: Any) -> None:
            raise TurnSuspended()

        api.on("turn_end", on_turn_end)

    session = _session(suspender)
    sub = Submission(text="from the bus", source="bus", submitter="nats_bus", submission_id="t-1")
    with patch("tau_agent_core.agent_loop.stream_simple", side_effect=_script(_text("ok"))):
        future = await asyncio.to_thread(lambda: session.submit_threadsafe(sub))
        result = await asyncio.wrap_future(future)

    assert result.accepted is True
    assert result.end_reason == "suspended"
    assert [m["role"] for m in result.messages] == ["user", "assistant"]


# ── runtime swallow detection (§3.2.2) ───────────────────────────────────────


def _swallowing_bus_handler(api: Any) -> None:
    """An extension whose ``ext:`` bus handler swallows the unwind in its own catch."""

    async def on_hit(payload: Any) -> None:
        try:
            mark_suspension_raised("tau-debugger")
            raise TurnSuspended()
        except BaseException:  # noqa: BLE001 — the defect under test
            pass

    api.on("ext:test_loop_suspension_contracts._producer:hit", on_hit)


def _producer(api: Any) -> None:
    async def on_call(event: dict[str, Any], ctx: Any) -> None:
        await api.emit("hit", {"tool": event["tool_name"]})

    api.on("tool_call", on_call)


async def test_a_swallowed_unwind_raises_naming_the_extension_and_runs_no_tool() -> None:
    ran: list[str] = []
    ends: list[Any] = []

    session = _session(
        _producer, _swallowing_bus_handler, _agent_ends(ends), tools=[_counting_tool("probe", ran)]
    )
    name = session._extension_runner._extensions[0].name
    with patch(
        "tau_agent_core.agent_loop.stream_simple", side_effect=_script(_named_calls("probe"))
    ):
        with pytest.raises(SuspensionSwallowedError) as raised:
            await session.prompt("go")

    assert raised.value.extension == name
    assert raised.value.event == "tool_call"
    assert raised.value.raised_by == "tau-debugger"
    assert name in str(raised.value)
    assert ran == []
    assert [e.end_reason for e in ends] == ["error"]


# ── the input hook's refuse (§3.9) ───────────────────────────────────────────


async def test_an_input_refuse_rejects_the_submission_and_writes_nothing() -> None:
    def refuser(api: Any) -> None:
        api.on("input", lambda e, ctx: {"refuse": "stepping is armed and nobody can answer"})

    session = _session(refuser)
    sub = Submission(text="go", source="interactive", submitter="human", submission_id="s1")
    with patch("tau_agent_core.agent_loop.stream_simple", side_effect=_script(_text("never"))):
        result = await session.submit(sub)

    assert result.accepted is False
    assert result.rejection_reason == "stepping is armed and nobody can answer"
    assert session.session_log.entries() == []


async def test_an_input_refuse_needs_a_reason() -> None:
    def refuser(api: Any) -> None:
        api.on("input", lambda e, ctx: {"refuse": ""})

    session = _session(refuser)
    sub = Submission(text="go", source="interactive", submitter="human", submission_id="s1")
    with pytest.raises(ValueError, match="non-empty string"):
        await session.submit(sub)

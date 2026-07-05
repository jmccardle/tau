"""E7 §3 (S50) — blocked-call rendering: veto marker on the event + JSON record.

A ``tool_call`` extension VETO is a DISTINCT presentation from a generic errored
tool result (anchor G11). S50 threads the block through two surfaces:

- the ``tool_execution_end`` :class:`AgentEvent` gains ``blocked=True`` +
  ``blocked_by=<ext>`` so a front-end can render "⛔ blocked by <ext>: <reason>"
  instead of a plain error box (the TUI surface, tested in tau-coding-agent);
- a parallel JSON record — ``{"type": "extension", "kind": "veto", "extension",
  "tool", "reason", "blocked": true}`` — rides the S49 record sink so an
  orchestrator reading a child ``tau -p --mode json`` stream can tell a veto from a
  tool error.

A NON-veto block (argument-validation failure) carries no attribution and stays a
generic errored result — no ``blocked`` marker, no veto record — so the distinct
render is reserved for real extension vetoes.

These run the FULL loop (default parallel mode) plus a direct sequential-mode
drive, proving both execution branches thread the marker. Only the network
boundary (``stream_simple``) is faked.

Reference: docs/EXTENSIONS-DEMO-ROADMAP.md §3 S50 (anchor G11).
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

from tau_ai.streaming import DoneEvent, TextDeltaEvent
from tau_ai.types import AssistantMessage, Model, TextContent, ToolCall, Usage

from tau_agent_core.agent_loop import AgentLoop
from tau_agent_core.agent_loop_types import AgentLoopConfig
from tau_agent_core.agent_session import AgentSession
from tau_agent_core.events import AgentEvent
from tau_agent_core.extension_types import ExtensionContext
from tau_agent_core.extensions.runner import ExtensionRunner
from tau_agent_core.session_log import InMemorySessionLog


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


def _text_assistant(text: str) -> AssistantMessage:
    return AssistantMessage(
        content=[TextContent(text=text)],
        api="openai-completions",
        provider="openai",
        model="gpt-4o",
        stop_reason="stop",
        timestamp=0,
        usage=Usage(),
    )


def _tool_call_assistant(call_id: str, name: str, args: dict[str, Any]) -> AssistantMessage:
    return AssistantMessage(
        content=[ToolCall(type="toolCall", id=call_id, name=name, arguments=args)],
        api="openai-completions",
        provider="openai",
        model="gpt-4o",
        stop_reason="toolUse",
        timestamp=0,
        usage=Usage(),
    )


class _Stream:
    """Minimal async stream matching the stream_simple contract."""

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

    async def result(self) -> Any:
        for event in self._events:
            if isinstance(event, DoneEvent):
                return event.final
        return None

    def abort(self) -> None:
        pass


def _has_tool_result(messages: list[Any], tool_name: str) -> bool:
    for m in messages:
        role = m.get("role") if isinstance(m, dict) else getattr(m, "role", None)
        name = m.get("tool_name") if isinstance(m, dict) else getattr(m, "tool_name", None)
        if role == "toolResult" and name == tool_name:
            return True
    return False


def _fake_stream_calling(tool_name: str, tool_args: dict[str, Any]):
    """Fake stream_simple: emit one tool call, then a text stop once the tool ran."""

    async def fake(model, context, options=None):
        messages = context.get("messages", []) if isinstance(context, dict) else []
        if _has_tool_result(messages, tool_name):
            final = _text_assistant("done")
            return _Stream(
                [
                    TextDeltaEvent(delta="done", partial=final),
                    DoneEvent(final=final, usage=Usage()),
                ]
            )
        final = _tool_call_assistant("call_1", tool_name, tool_args)
        return _Stream([DoneEvent(final=final, usage=Usage())])

    return fake


def _make_session(*extensions) -> AgentSession:
    return AgentSession(
        session_log=InMemorySessionLog(),
        model=_model(),
        extensions=list(extensions),
    )


# ---------------------------------------------------------------------------
# Parallel path (the default execution mode) — through a real AgentSession
# ---------------------------------------------------------------------------


async def test_veto_marks_event_and_emits_veto_record() -> None:
    """A ``tool_call`` veto marks the end event blocked AND emits a JSON veto record."""

    async def probe_execute(tool_call_id, params, signal, on_update, ctx):
        raise AssertionError("vetoed tool must not execute")

    def ext(api) -> None:
        api.register_tool(
            {
                "name": "guarded",
                "description": "a guarded tool",
                "parameters": {"type": "object", "properties": {}, "required": []},
                "execute": probe_execute,
            }
        )

    session = _make_session(ext)
    session._extension_runner.register_extension("/x/30_permission_gate.py").on(
        "tool_call",
        lambda event, ctx: {"block": True, "reason": "denied by policy"},
    )

    # The JSON-stream record sink (S49): the veto record lands here (headless json).
    records: list[dict[str, Any]] = []
    session.set_extension_record_sink(records.append)

    # Capture the tool_execution_end event's blocked marker.
    ends: list[AgentEvent] = []

    def capture(event: AgentEvent) -> None:
        if event.type == "tool_execution_end":
            ends.append(event)

    session.subscribe(capture)

    with patch(
        "tau_agent_core.agent_loop.stream_simple",
        side_effect=_fake_stream_calling("guarded", {}),
    ):
        await session.prompt("call the guarded tool")

    # Surface 1 — the event carries the distinct blocked marker + attribution.
    blocked_ends = [e for e in ends if e.blocked]
    assert len(blocked_ends) == 1
    assert blocked_ends[0].blocked_by == "/x/30_permission_gate.py"
    assert blocked_ends[0].is_error is True
    assert blocked_ends[0].tool_name == "guarded"

    # Surface 2 — the JSON veto record carries blocked: true + attribution.
    assert records == [
        {
            "type": "extension",
            "kind": "veto",
            "extension": "/x/30_permission_gate.py",
            "tool": "guarded",
            "reason": "denied by policy",
            "blocked": True,
        }
    ]


async def test_arg_validation_block_is_not_a_veto() -> None:
    """An argument-validation block is a generic error — no ``blocked`` marker, no record.

    Only a `tool_call` extension VETO gets the distinct "⛔ blocked by <ext>" render;
    a bad-argument block stays an ordinary errored tool result.
    """

    async def probe_execute(tool_call_id, params, signal, on_update, ctx):
        return {"content": [{"type": "text", "text": "ran"}]}

    def ext(api) -> None:
        api.register_tool(
            {
                "name": "strict",
                "description": "requires q",
                "parameters": {
                    "type": "object",
                    "properties": {"q": {"type": "string"}},
                    "required": ["q"],  # the LLM omits it → validation block
                },
                "execute": probe_execute,
            }
        )

    session = _make_session(ext)
    records: list[dict[str, Any]] = []
    session.set_extension_record_sink(records.append)

    ends: list[AgentEvent] = []

    def capture(event: AgentEvent) -> None:
        if event.type == "tool_execution_end":
            ends.append(event)

    session.subscribe(capture)

    with patch(
        "tau_agent_core.agent_loop.stream_simple",
        side_effect=_fake_stream_calling("strict", {}),  # no "q"
    ):
        await session.prompt("call strict without q")

    # It errored, but it is NOT a veto: no blocked marker and no veto record.
    error_ends = [e for e in ends if e.is_error]
    assert error_ends, "the missing-arg call should have errored"
    assert all(e.blocked is False for e in error_ends)
    assert all(e.blocked_by is None for e in error_ends)
    assert records == []


# ---------------------------------------------------------------------------
# Sequential path — driven directly so BOTH execution branches are proven
# ---------------------------------------------------------------------------


async def test_sequential_mode_also_marks_the_veto() -> None:
    """The sequential execution branch threads the same blocked marker + record."""
    records: list[dict[str, Any]] = []
    ctx = ExtensionContext()
    ctx.set_record_sink(records.append)

    runner = ExtensionRunner(context=ctx)
    runner.register_extension("mem:gate").on(
        "tool_call",
        lambda event, ctx: {"block": True, "reason": "nope"},
    )

    emitted: list[AgentEvent] = []

    async def emit(event: AgentEvent) -> None:
        emitted.append(event)

    loop = AgentLoop(
        config=AgentLoopConfig(tool_execution_mode="sequential"),
        emit=emit,
        hook_dispatcher=runner,
    )

    assistant = _tool_call_assistant("c1", "guarded", {})
    await loop._execute_tool_calls(
        assistant,
        [ToolCall(type="toolCall", id="c1", name="guarded", arguments={})],
    )

    ends = [e for e in emitted if e.type == "tool_execution_end"]
    assert len(ends) == 1
    assert ends[0].blocked is True
    assert ends[0].blocked_by == "mem:gate"
    assert records == [
        {
            "type": "extension",
            "kind": "veto",
            "extension": "mem:gate",
            "tool": "guarded",
            "reason": "nope",
            "blocked": True,
        }
    ]

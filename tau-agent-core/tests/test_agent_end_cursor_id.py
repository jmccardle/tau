"""Every ``agent_start`` and ``agent_end`` names the cursor it belongs to (Forgejo #27).

The tau-router extension attributes a hold by ``event.cursor_id`` and skips the hold
when it is ``None``. Three ``agent_end`` emits once went straight to the bus and
skipped the stamp; each is driven here, plus the loop's own as the control:

* the loop's ``agent_end`` — an ordinary ``prompt()``;
* ``_compact_mid_turn`` — the hard limit crossed inside a turn;
* ``_maybe_auto_compact`` — the soft limit crossed after a turn;
* ``compact()`` — the public call, which a deferred ``ctx.compact`` also reaches.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

from tau_llm.streaming import DoneEvent
from tau_llm.types import AssistantMessage, Model, TextContent, ToolCall, Usage

from tau_agent_core.agent_session import AgentSession
from tau_agent_core.compaction import CompactionSettings
from tau_agent_core.events import AgentEvent
from tau_agent_core.session_log import InMemorySessionLog
from tau_agent_core.tools.base import AgentTool, AgentToolResult, ToolDefinition

_TS = 1_700_000_000_000
_SETTINGS = CompactionSettings(
    hard_limit_tokens=6_000,
    soft_limit_tokens=5_000,
    keep_recent_tokens=800,
    reserve_tokens=1_000,
)


class _Stream:
    """Minimal async stream matching the ``stream_simple`` contract."""

    def __init__(self, final: AssistantMessage) -> None:
        self._final = final

    def __aiter__(self) -> "_Stream":
        self._sent = False
        return self

    async def __anext__(self) -> Any:
        if self._sent:
            raise StopAsyncIteration
        self._sent = True
        return DoneEvent(final=self._final, usage=self._final.usage or Usage())

    async def result(self) -> Any:
        return self._final

    def abort(self) -> None:
        pass


def _assistant(content: list[Any], stop_reason: str, billed: int = 0) -> AssistantMessage:
    usage = Usage(input_tokens=billed, output_tokens=10, total_tokens=billed + 10)
    return AssistantMessage(
        content=content,
        api="openai-completions",
        provider="openai",
        model="test-model",
        stop_reason=stop_reason,
        timestamp=_TS,
        usage=usage if billed else Usage(),
    )


def _tool_call(call_id: str) -> AssistantMessage:
    call = ToolCall(type="toolCall", id=call_id, name="bulk", arguments={"size": 9000})
    return _assistant([call], "toolUse")


def _text(text: str, billed: int = 0) -> AssistantMessage:
    return _assistant([TextContent(text=text)], "stop", billed)


async def _summary(model, context, options=None, **_):
    return _text("## Goal\nSUMMARY", billed=500)


async def _bulk(tool_call_id: str, args: dict[str, Any], signal: Any = None) -> AgentToolResult:
    return AgentToolResult(
        tool_name="bulk",
        tool_call_id=tool_call_id,
        content=[{"type": "text", "text": "lorem ipsum " * (int(args["size"]) // 12)}],
    )


def _session() -> AgentSession:
    bulk = AgentTool(
        definition=ToolDefinition(
            name="bulk",
            label="Bulk",
            description="Return a block of text of a requested size.",
            parameters={
                "type": "object",
                "properties": {"size": {"type": "integer"}},
                "required": ["size"],
            },
            execute=_bulk,
            execution_mode="sequential",
        )
    )
    model = Model(
        id="test-model",
        name="Test",
        api="openai-completions",
        provider="openai",
        base_url="http://example.invalid/v1",
        context_window=40_000,
        max_tokens=4096,
    )
    return AgentSession(
        session_log=InMemorySessionLog(),
        model=model,
        compaction_settings=_SETTINGS,
        tools=[bulk],
    )


async def _seed(session: AgentSession, turns: int = 4) -> None:
    """Give the log enough history for the cut-point search to have a prefix to fold."""
    for i in range(turns):
        user = {"role": "user", "content": [{"type": "text", "text": "u" * 3000}]}
        reply = {"role": "assistant", "content": [{"type": "text", "text": f"a{i}"}]}
        await session.cursor.append_message({**user, "timestamp": _TS + i})
        await session.cursor.append_message({**reply, "timestamp": _TS + 100 + i})


def _record(session: AgentSession) -> list[AgentEvent]:
    events: list[AgentEvent] = []
    session.subscribe(events.append)
    return events


def _assert_brackets_name_the_head(events: list[AgentEvent], session: AgentSession) -> None:
    brackets = [e for e in events if e.type in ("agent_start", "agent_end")]
    assert brackets, "no agent_start / agent_end was emitted at all"
    unstamped = [e.type for e in brackets if e.cursor_id is None]
    assert not unstamped, f"events without a cursor_id: {unstamped}"
    assert {e.cursor_id for e in brackets} == {session.cursor.id}


async def test_the_loops_own_agent_end_carries_cursor_id() -> None:
    session = _session()
    events = _record(session)
    with patch("tau_agent_core.agent_loop.stream_simple", return_value=_Stream(_text("hi"))):
        await session.prompt("hello")
    _assert_brackets_name_the_head(events, session)


async def test_a_mid_turn_compaction_brackets_carry_cursor_id(monkeypatch) -> None:
    monkeypatch.setattr("tau_agent_core.compaction.complete_simple", _summary)
    session = _session()
    events = _record(session)
    responses = [_tool_call(f"c{i}") for i in range(6)] + [_text("done", billed=1200)]
    sent: list[int] = []

    def fake(model, context, options=None, **kwargs):
        sent.append(1)
        return _Stream(responses[min(len(sent) - 1, len(responses) - 1)])

    with patch("tau_agent_core.agent_loop.stream_simple", side_effect=fake):
        await session.prompt("start")

    assert any(e["type"] == "compaction" for e in session.session_log.entries())
    ends = [e for e in events if e.type == "agent_end"]
    assert len(ends) >= 2, "expected the compaction's agent_end besides the loop's"
    _assert_brackets_name_the_head(events, session)


async def test_an_end_of_turn_compaction_brackets_carry_cursor_id(monkeypatch) -> None:
    monkeypatch.setattr("tau_agent_core.compaction.complete_simple", _summary)
    session = _session()
    await _seed(session)
    events = _record(session)
    with patch(
        "tau_agent_core.agent_loop.stream_simple", return_value=_Stream(_text("done", billed=9000))
    ):
        await session.prompt("go")

    assert any(e["type"] == "compaction" for e in session.session_log.entries())
    assert len([e for e in events if e.type == "agent_end"]) == 2
    _assert_brackets_name_the_head(events, session)


async def test_a_manual_compact_brackets_carry_cursor_id(monkeypatch) -> None:
    monkeypatch.setattr("tau_agent_core.compaction.complete_simple", _summary)
    session = _session()
    await _seed(session)
    events = _record(session)
    await session.compact()

    assert any(e["type"] == "compaction" for e in session.session_log.entries())
    assert [e.type for e in events if e.type.startswith("agent_")] == ["agent_start", "agent_end"]
    _assert_brackets_name_the_head(events, session)

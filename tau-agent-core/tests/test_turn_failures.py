"""A turn that fails writes what it produced, and says why it stopped.

Reference: docs/TURN-FAILURES.md. §1 is the ``turn_error`` marker, §2 the stream
that fails partway, §3 the two exceptions a tool raises on purpose.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import patch

import pytest

from tau_agent_core.agent_loop import UNRUN_TOOL_RESULT, completed_messages
from tau_agent_core.agent_session import AgentSession
from tau_agent_core.messages import convert_to_llm
from tau_agent_core.session_log import InMemorySessionLog, is_incomplete
from tau_agent_core.tools.base import INTERNAL_TOOL_ERROR, AgentTool, ToolError, ToolHalt
from tau_llm.streaming import (
    DoneEvent,
    ErrorEvent,
    TextDeltaEvent,
    ThinkingDeltaEvent,
    ToolCallDeltaEvent,
)
from tau_llm.tools import ToolDefinition
from tau_llm.types import (
    AssistantMessage,
    Model,
    TextContent,
    ThinkingContent,
    ToolCall,
    Usage,
)

#: A fixed epoch-ms stamp for fixtures — never 0 (docs/MESSAGE-TIMESTAMPS.md §2).
_TS = 1_700_000_000_000


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


def _session(**kwargs: Any) -> AgentSession:
    return AgentSession(session_log=InMemorySessionLog(), model=_model(), **kwargs)


def _assistant(content: list[Any], stop_reason: str = "stop") -> AssistantMessage:
    return AssistantMessage(
        content=content,
        api="openai-completions",
        provider="openai",
        model="gpt-4o",
        stop_reason=stop_reason,
        timestamp=_TS,
        usage=Usage(input_tokens=1, output_tokens=1, total_tokens=2),
    )


class _Stream:
    """An async-iterable ``stream_simple`` result: fixed events, then maybe a raise."""

    def __init__(self, events: list[Any], raises: BaseException | None = None) -> None:
        self._events = events
        self._raises = raises

    def __aiter__(self):
        return self._iter()

    async def _iter(self):
        for event in self._events:
            yield event
        if self._raises is not None:
            raise self._raises


def _streams(*streams: _Stream, captured: list | None = None):
    """A ``stream_simple`` stand-in returning ``streams`` in order, recording each context."""
    queue = list(streams)

    async def _fake(model, context, options=None):
        if captured is not None:
            captured.append(context)
        return queue.pop(0)

    return _fake


def _text_deltas(*pieces: str) -> list[TextDeltaEvent]:
    """Text deltas whose ``partial`` accumulates, as every provider's does."""
    events, text = [], ""
    for piece in pieces:
        text += piece
        events.append(TextDeltaEvent(delta=piece, partial=_assistant([TextContent(text=text)])))
    return events


def _tool(name: str, raises: BaseException | None = None) -> AgentTool:
    async def _execute(**kw: Any) -> str:
        if raises is not None:
            raise raises
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


def _messages(log: InMemorySessionLog) -> list[dict]:
    return [e["message"] for e in log.entries() if e.get("type") in ("message", "customMessage")]


def _call_then_text(call_id: str, name: str) -> list[_Stream]:
    return [
        _Stream(
            [
                DoneEvent(
                    final=_assistant(
                        [ToolCall(id=call_id, name=name, arguments={})], stop_reason="toolUse"
                    ),
                    usage=Usage(),
                )
            ]
        ),
        _Stream([DoneEvent(final=_assistant([TextContent(text="done")]), usage=Usage())]),
    ]


def test_a_stream_that_errors_partway_keeps_its_text_as_an_error_message():
    """§2: the streamed text is finalized, marked ``error``, with the reason."""
    session = _session()
    stream = _Stream([*_text_deltas("The answer ", "is 4"), ErrorEvent(message="upstream 503")])
    with patch("tau_agent_core.agent_loop.stream_simple", side_effect=_streams(stream)):
        with pytest.raises(RuntimeError, match="upstream 503") as excinfo:
            asyncio.run(session.prompt("what is 2+2"))

    assert not any(is_incomplete(e) for e in session.session_log.entries())
    user, assistant, marker = _messages(session.session_log)
    assert user["role"] == "user"
    assert assistant["content"] == [{"type": "text", "text": "The answer is 4"}]
    assert assistant["stop_reason"] == "error"
    assert assistant["error_message"] == "upstream 503"
    assert marker["customType"] == "turn_error"
    (kept,) = completed_messages(excinfo.value)
    assert kept.stop_reason == "error"


def test_the_model_reads_the_cut_message_on_the_next_turn():
    """§2: an interrupted message is a valid message, so nothing filters it out."""
    session = _session()
    captured: list = []
    fake = _streams(
        _Stream([*_text_deltas("partial"), ErrorEvent(message="upstream 503")]),
        _Stream([DoneEvent(final=_assistant([TextContent(text="again")]), usage=Usage())]),
        captured=captured,
    )
    with patch("tau_agent_core.agent_loop.stream_simple", side_effect=fake):
        with pytest.raises(RuntimeError):
            asyncio.run(session.prompt("first"))
        asyncio.run(session.prompt("continue"))

    sent = captured[1]["messages"]
    roles = [m["role"] if isinstance(m, dict) else m.role for m in sent]
    assert roles[-3:] == ["user", "assistant", "user"]
    assert "turn_error" not in str(sent)


def test_a_tool_call_cut_short_is_answered_so_the_next_request_is_valid():
    """§2: a call with no result is a transcript every provider rejects."""
    call = ToolCall(id="c1", name="probe", arguments={"path": "a"})
    session = _session(tools=[_tool("probe")])
    stream = _Stream(
        [
            ToolCallDeltaEvent(delta={"index": 0}, partial=_assistant([call])),
            ErrorEvent(message="connection reset"),
        ]
    )
    with patch("tau_agent_core.agent_loop.stream_simple", side_effect=_streams(stream)):
        with pytest.raises(RuntimeError, match="connection reset"):
            asyncio.run(session.prompt("go"))

    _, assistant, result, marker = _messages(session.session_log)
    assert assistant["content"][0]["id"] == "c1"
    assert result["tool_call_id"] == "c1"
    assert result["is_error"] is True
    assert result["content"] == [{"type": "text", "text": UNRUN_TOOL_RESULT}]
    assert marker["customType"] == "turn_error"


def test_a_stream_that_stops_without_done_is_finalized_too():
    """§2: an iterator that simply ends is a failure, and its partial is kept."""
    session = _session()
    thinking = ThinkingDeltaEvent(
        delta="hmm", partial=_assistant([ThinkingContent(thinking="hmm")])
    )
    with patch(
        "tau_agent_core.agent_loop.stream_simple",
        side_effect=_streams(_Stream([thinking])),
    ):
        with pytest.raises(RuntimeError, match="without a DoneEvent"):
            asyncio.run(session.prompt("go"))

    _, assistant, _marker = _messages(session.session_log)
    assert assistant["stop_reason"] == "error"
    assert assistant["content"][0]["thinking"] == "hmm"


def test_an_exception_out_of_the_stream_keeps_its_type_and_the_partial():
    """§2: the caller catches what was raised, and the store still has the text."""
    session = _session()
    stream = _Stream(_text_deltas("so far"), raises=ValueError("bad chunk"))
    with patch("tau_agent_core.agent_loop.stream_simple", side_effect=_streams(stream)):
        with pytest.raises(ValueError, match="bad chunk"):
            asyncio.run(session.prompt("go"))

    _, assistant, marker = _messages(session.session_log)
    assert assistant["content"] == [{"type": "text", "text": "so far"}]
    assert assistant["error_message"] == "ValueError: bad chunk"
    assert marker["details"] == {"exception": "ValueError: bad chunk"}


def test_turn_error_is_shown_and_never_sent():
    """§1: the marker is on the path for a head, and off the wire for the model."""
    session = _session()
    with patch(
        "tau_agent_core.agent_loop.stream_simple",
        side_effect=_streams(_Stream([ErrorEvent(message="upstream 503")])),
    ):
        with pytest.raises(RuntimeError):
            asyncio.run(session.prompt("go"))

    marker = _messages(session.session_log)[-1]
    assert marker["display"] is True
    assert marker["content"] == [
        {"type": "text", "text": "The turn failed: RuntimeError: upstream 503"}
    ]
    assert convert_to_llm([marker]) == []


def test_a_tool_error_is_read_by_the_model_and_the_turn_goes_on():
    """§3: ``ToolError``'s message is the result the model reads."""
    session = _session(tools=[_tool("probe", raises=ToolError("no such file: a.txt"))])
    with patch(
        "tau_agent_core.agent_loop.stream_simple",
        side_effect=_streams(*_call_then_text("c1", "probe")),
    ):
        messages = asyncio.run(session.prompt("go"))

    result = next(m for m in messages if m["role"] == "toolResult")
    assert result["is_error"] is True
    assert result["content"] == [{"type": "text", "text": "no such file: a.txt"}]
    assert result["details"] is None
    assert messages[-1]["content"][0]["text"] == "done"


def test_a_tool_halt_ends_the_turn_after_its_batch():
    """§3: ``ToolHalt`` answers the call, then no further request is made."""
    session = _session(tools=[_tool("probe", raises=ToolHalt("the arm is not homed"))])
    captured: list = []
    with patch(
        "tau_agent_core.agent_loop.stream_simple",
        side_effect=_streams(*_call_then_text("c1", "probe"), captured=captured),
    ):
        messages = asyncio.run(session.prompt("go"))

    assert len(captured) == 1
    assert [m["role"] for m in messages] == ["user", "assistant", "toolResult"]
    assert messages[-1]["content"] == [{"type": "text", "text": "the arm is not homed"}]
    assert messages[-1]["is_error"] is True


def test_any_other_exception_is_shown_to_the_user_and_not_the_model():
    """§3: a bug in the tool or in τ gives the model a fixed sentence."""
    session = _session(tools=[_tool("probe", raises=KeyError("cfg"))])
    captured: list = []
    with patch(
        "tau_agent_core.agent_loop.stream_simple",
        side_effect=_streams(*_call_then_text("c1", "probe"), captured=captured),
    ):
        messages = asyncio.run(session.prompt("go"))

    result = next(m for m in messages if m["role"] == "toolResult")
    assert result["content"] == [{"type": "text", "text": INTERNAL_TOOL_ERROR}]
    assert result["details"] == {"exception": "KeyError: 'cfg'"}
    assert messages[-1]["content"][0]["text"] == "done"

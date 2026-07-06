"""E3-ctx / step S20 — turn-end deferral + the send_user_message injection queue.

Verifies decision 3 (mid-turn compact/fork defers to the tail of ``prompt()``,
applied exactly once, never mid-turn) and the ``send_user_message`` queue
(``followUp`` re-enters the loop within the same ``prompt()``; ``nextTurn`` lands
on the next ``prompt()``).

All three run the FULL agent loop; only the network boundary (``stream_simple``)
is faked, and the fake emits a real tool call so the deferring / queuing tool
actually executes mid-turn — the behavior is exercised through the real loop, not
by poking private queues directly. The LLM-backed compaction summarizer is patched
so the deferred compact appends without a network call.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest

from tau_ai.streaming import DoneEvent, TextDeltaEvent
from tau_ai.types import AssistantMessage, Model, TextContent, ToolCall, Usage

from tau_agent_core.agent_session import AgentSession
from tau_agent_core.compaction import CompactionSettings
from tau_agent_core.session_log import InMemorySessionLog


# ── fakes ────────────────────────────────────────────────────────────────────


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
    """Emit one tool call, then a plain text stop once that tool has run."""

    async def fake(model, context, options=None):
        messages = context.get("messages", []) if isinstance(context, dict) else []
        if _has_tool_result(messages, tool_name):
            final = _text_assistant("done")
            return _Stream(
                [TextDeltaEvent(delta="done", partial=final), DoneEvent(final=final, usage=Usage())]
            )
        final = _tool_call_assistant("call_1", tool_name, tool_args)
        return _Stream([DoneEvent(final=final, usage=Usage())])

    return fake


async def _fake_stream_text(model, context, options=None):
    """Always a plain text stop — no tool call (used for a follow-on prompt)."""
    final = _text_assistant("done")
    return _Stream(
        [TextDeltaEvent(delta="done", partial=final), DoneEvent(final=final, usage=Usage())]
    )


def _summary_response(text: str):
    async def _impl(model, context, options=None):
        return AssistantMessage(
            content=[TextContent(text=text)],
            api="openai-completions",
            provider="openai",
            model="gpt-4o",
            stop_reason="stop",  # type: ignore[arg-type]
            timestamp=0,
        )

    return _impl


def _text(message: dict) -> str:
    content = message.get("content", "")
    if isinstance(content, str):
        return content
    return " ".join(
        b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"
    )


def _user_texts(messages: list[dict]) -> list[str]:
    return [_text(m) for m in messages if m.get("role") == "user"]


def _make_session(*extensions, settings: CompactionSettings | None = None) -> AgentSession:
    return AgentSession(
        session_log=InMemorySessionLog(),
        model=_model(),
        extensions=list(extensions),
        compaction_settings=settings or CompactionSettings(enabled=False),
    )


# ── deferred compact ─────────────────────────────────────────────────────────


async def test_deferred_compact_applies_once_at_end_of_prompt(monkeypatch) -> None:
    """A tool's ``ctx.compact(defer=True)`` compacts once at prompt()'s tail.

    Mid-turn (while the tool runs, under the live loop) NO compaction entry
    exists; exactly one is appended after ``prompt()`` returns — proving the
    intent was deferred, not applied mid-turn, and applied exactly once.
    """
    monkeypatch.setattr(
        "tau_agent_core.compaction.complete_simple", _summary_response("DEFCOMPACT")
    )

    observed: dict[str, int] = {}

    def ext(api) -> None:
        async def execute(tool_call_id, params, signal, on_update, ctx):
            # Under the live loop: the deferred request must NOT compact yet.
            observed["mid_turn"] = sum(1 for e in ctx.entries() if e["type"] == "compaction")
            result = await ctx.compact(defer=True)
            # A deferred compact only records intent — no result, no entry.
            observed["deferred_result_is_none"] = 1 if result is None else 0
            observed["after_defer"] = sum(1 for e in ctx.entries() if e["type"] == "compaction")
            return {"content": [{"type": "text", "text": "deferred"}]}

        api.register_tool(
            {
                "name": "defer_compact",
                "description": "defer a compaction to end-of-prompt",
                "parameters": {"type": "object", "properties": {}, "required": []},
                "execute": execute,
            }
        )

    # Large window keeps auto-compaction dormant; keep_recent_tokens=1 makes the
    # DEFERRED compact cut almost everything so it definitely appends.
    session = _make_session(
        ext, settings=CompactionSettings(enabled=True, keep_recent_tokens=1)
    )
    # Seed prior turns so the compaction has an ample prefix to summarize.
    log = session.session_log
    for i in range(2):
        log.append_message({"role": "user", "content": [{"type": "text", "text": f"u{i}"}]})
        log.append_message(
            {"role": "assistant", "content": [{"type": "text", "text": f"a{i}"}]}
        )

    def compaction_count() -> int:
        return sum(1 for e in log.entries() if e["type"] == "compaction")

    assert compaction_count() == 0

    with patch(
        "tau_agent_core.agent_loop.stream_simple",
        side_effect=_fake_stream_calling("defer_compact", {}),
    ):
        await session.prompt("please compact when you're done")

    # Mid-turn there was no compaction; the deferred call returned None and added
    # no entry; exactly one compaction exists after the prompt.
    assert observed["mid_turn"] == 0
    assert observed["after_defer"] == 0
    assert observed["deferred_result_is_none"] == 1
    assert compaction_count() == 1
    entries = log.entries()
    assert entries[-1]["type"] == "compaction"
    assert "DEFCOMPACT" in entries[-1]["summary"]


# ── followUp ─────────────────────────────────────────────────────────────────


async def test_follow_up_reenters_within_same_prompt() -> None:
    """``send_user_message(..., "followUp")`` re-enters the loop this prompt()."""

    def ext(api) -> None:
        async def execute(tool_call_id, params, signal, on_update, ctx):
            api.send_user_message("the follow up question", deliver_as="followUp")
            return {"content": [{"type": "text", "text": "queued follow up"}]}

        api.register_tool(
            {
                "name": "queue_fu",
                "description": "queue a follow-up message",
                "parameters": {"type": "object", "properties": {}, "required": []},
                "execute": execute,
            }
        )

    session = _make_session(ext)

    with patch(
        "tau_agent_core.agent_loop.stream_simple",
        side_effect=_fake_stream_calling("queue_fu", {}),
    ):
        returned = await session.prompt("go")

    # The single prompt() call ran BOTH turns: the original "go" and the
    # re-entered "the follow up question".
    returned_users = _user_texts([m for m in returned if isinstance(m, dict)])
    assert "go" in returned_users
    assert "the follow up question" in returned_users

    # The follow-up landed in the one authoritative session, after the first turn.
    session_users = _user_texts(session.messages)
    assert session_users == ["go", "the follow up question"]

    # The queue is drained — no leftover follow-up, and none leaked to nextTurn.
    assert session._pending_follow_up_messages == []
    assert session._pending_next_turn_messages == []


# ── nextTurn ─────────────────────────────────────────────────────────────────


async def test_next_turn_lands_on_the_next_prompt() -> None:
    """``send_user_message(..., "nextTurn")`` is held until the NEXT prompt()."""

    def ext(api) -> None:
        async def execute(tool_call_id, params, signal, on_update, ctx):
            api.send_user_message("saved for later", deliver_as="nextTurn")
            return {"content": [{"type": "text", "text": "queued next turn"}]}

        api.register_tool(
            {
                "name": "queue_nt",
                "description": "queue a next-turn message",
                "parameters": {"type": "object", "properties": {}, "required": []},
                "execute": execute,
            }
        )

    session = _make_session(ext)

    # Prompt 1: the tool queues a nextTurn message. It must NOT be delivered now.
    with patch(
        "tau_agent_core.agent_loop.stream_simple",
        side_effect=_fake_stream_calling("queue_nt", {}),
    ):
        await session.prompt("first")

    assert "saved for later" not in _user_texts(session.messages)
    assert session._pending_next_turn_messages == ["saved for later"]

    # Prompt 2 (plain text, no new queuing): the pending nextTurn message is
    # injected alongside the "second" user turn and drained.
    with patch("tau_agent_core.agent_loop.stream_simple", side_effect=_fake_stream_text):
        returned = await session.prompt("second")

    assert session._pending_next_turn_messages == []
    session_users = _user_texts(session.messages)
    assert "saved for later" in session_users
    # Order: the injected nextTurn message follows its triggering user turn.
    assert session_users == ["first", "second", "saved for later"]
    returned_users = _user_texts([m for m in returned if isinstance(m, dict)])
    assert "second" in returned_users
    assert "saved for later" in returned_users


# ── validation ───────────────────────────────────────────────────────────────


async def test_queue_message_rejects_unknown_deliver_as() -> None:
    """The session-side seam validates deliver_as (Fail-Early, no silent drop)."""
    session = _make_session()
    with pytest.raises(ValueError, match="followUp"):
        session._queue_message("x", deliver_as="steer")
    assert session._pending_follow_up_messages == []
    assert session._pending_next_turn_messages == []

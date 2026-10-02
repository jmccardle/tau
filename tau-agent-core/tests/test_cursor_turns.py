"""Turns run per cursor (docs/CURSORS.md §2, §6, §7).

Each cursor carries its own turn lock, abort signal and queues, so one session
runs turns on two cursors at once; every event names the cursor whose turn
emitted it; aborting a cursor aborts the cursors it owns; and an extension
handler sees the signal of the turn it runs in.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import patch

import pytest

from tau_llm.streaming import DoneEvent, TextDeltaEvent
from tau_llm.types import AssistantMessage, Model, TextContent, Usage
from tau_agent_core.agent_session import AgentSession
from tau_agent_core.events import AgentEvent
from tau_agent_core.session_log import InMemorySessionLog
from tau_agent_core.submission import Submission

_TS = 1_700_000_000_000


def _model() -> Model:
    return Model(
        id="m",
        provider="openai",
        api="openai-completions",
        base_url="http://127.0.0.1:1/v1",
        name="m",
        context_window=8192,
        max_tokens=256,
    )


def _assistant(text: str) -> AssistantMessage:
    return AssistantMessage(
        content=[TextContent(text=text)],
        api="openai-completions",
        provider="openai",
        model="m",
        stop_reason="stop",
        timestamp=_TS,
        usage=Usage(input_tokens=1, output_tokens=1, total_tokens=2),
    )


class _Stream:
    def __init__(self, text: str) -> None:
        self._message = _assistant(text)

    def __aiter__(self):
        async def _gen():
            yield TextDeltaEvent(delta=self._message.content[0].text, partial=self._message)
            yield DoneEvent(final=self._message, usage=self._message.usage)

        return _gen()

    async def result(self) -> AssistantMessage:
        return self._message

    def abort(self) -> None:
        pass


class _Gated:
    """A provider that holds every call open until its gate is set."""

    def __init__(self) -> None:
        self.gate = asyncio.Event()
        self.calls = 0

    async def stream(self, model: Any, context: Any, options: Any = None) -> _Stream:
        self.calls += 1
        await self.gate.wait()
        last = context["messages"][-1]
        content = last.get("content") if isinstance(last, dict) else last.content
        first = content if isinstance(content, str) else content[0]
        text = first if isinstance(first, str) else getattr(first, "text", None) or first["text"]
        return _Stream(f"re: {text}")


def _sub(text: str) -> Submission:
    return Submission(
        text=text,
        source="interactive",
        submitter="human",
        submission_id=f"s-{text}",
        multitask_strategy="enqueue",
    )


def _session() -> AgentSession:
    return AgentSession(session_log=InMemorySessionLog(), model=_model(), tools=[])


async def test_two_cursors_run_turns_concurrently_on_one_session():
    session = _session()
    second = await session.open_cursor(None, owner=session.cursor, label="second")
    provider = _Gated()

    with patch("tau_agent_core.agent_loop.stream_simple", side_effect=provider.stream):
        head_turn = asyncio.create_task(session.submit(_sub("head")))
        other_turn = asyncio.create_task(session.submit(_sub("other"), cursor=second))
        while provider.calls < 2:
            await asyncio.sleep(0)
        assert session.cursor.busy and second.busy, "both turns are in flight at once"
        provider.gate.set()
        await asyncio.gather(head_turn, other_turn)

    assert [m["role"] for m in session.cursor.context()] == ["user", "assistant"]
    assert "re: head" in repr(session.cursor.context())
    assert "re: other" in repr(second.context())
    assert "other" not in repr(session.cursor.context()), "each cursor kept its own path"


async def test_every_turn_event_names_its_cursor():
    session = _session()
    second = await session.open_cursor(None, label="second")
    seen: list[AgentEvent] = []
    session.subscribe(seen.append)

    with patch("tau_agent_core.agent_loop.stream_simple", return_value=_Stream("ok")):
        await session.submit(_sub("head"))
        await session.submit(_sub("other"), cursor=second)

    by_submission = {e.submission_id: e.cursor_id for e in seen}
    assert by_submission["s-head"] == session.cursor.id
    assert by_submission["s-other"] == second.id
    assert all(e.cursor_id is not None for e in seen)


async def test_a_turn_on_a_cursor_this_session_did_not_open_is_refused():
    session = _session()
    stranger = _session().cursor
    with pytest.raises(ValueError, match="not opened by this session"):
        await session.submit(_sub("x"), cursor=stranger)


async def test_aborting_a_cursor_aborts_the_cursors_it_owns_first():
    session = _session()
    child = await session.open_cursor(None, owner=session.cursor, label="child")
    grandchild = await session.open_cursor(None, owner=child, label="grandchild")
    bystander = await session.open_cursor(None, label="bystander")

    session.abort(session.cursor)

    assert child.abort_signal.is_aborted()
    assert grandchild.abort_signal.is_aborted()
    assert session.cursor.abort_signal.is_aborted()
    assert not bystander.abort_signal.is_aborted(), "only what the cursor owns"


async def test_closing_the_heads_cursor_is_refused():
    session = _session()
    with pytest.raises(ValueError, match="head"):
        await session.close_cursor(session.cursor)


async def test_open_and_close_announce_themselves_on_the_bus():
    session = _session()
    events: list[tuple[str, str]] = []
    session._events.on("cursor_open", lambda cursor: events.append(("open", cursor.id)))
    session._events.on("cursor_close", lambda cursor: events.append(("close", cursor.id)))

    cursor = await session.open_cursor(None, label="x")
    await session.close_cursor(cursor)

    assert events == [("open", cursor.id), ("close", cursor.id)]
    assert cursor not in session.cursors


async def test_a_handler_sees_the_signal_of_the_turn_it_runs_in():
    """docs/CURSORS.md §7: one context, resolved per turn through TURN_CURSOR."""
    session = _session()
    second = await session.open_cursor(None, label="second")
    signals: dict[str, Any] = {}

    def on_turn_end(event, ctx):
        signals[ctx.cursor.id] = ctx.signal

    session._extension_runner.register_extension("mem:probe").on("turn_end", on_turn_end)

    with patch("tau_agent_core.agent_loop.stream_simple", return_value=_Stream("ok")):
        await session.submit(_sub("head"))
        await session.submit(_sub("other"), cursor=second)

    assert signals[session.cursor.id] is session.cursor.abort_signal
    assert signals[second.id] is second.abort_signal

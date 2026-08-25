"""An interrupted turn keeps what it finished.

Reference: docs/PLAN-0.9.4.md §3 ("`Esc` loses the turn").

The requirement, in the words it was reported in:

    every complete message or tool result should be persisted, and the partial
    one that gets interrupted should be recovered to the extent possible.

Note what that does *not* say. It concedes that a half-streamed assistant
message may be unrecoverable. It insists that the **complete** ones survive,
which is the much easier bar that was being missed.

Two defects sat behind it, and they are independent:

1. ``AgentSession._run_one_turn`` persisted the whole turn as one batch AFTER
   ``loop.run`` returned. The loop accumulates into a local, so a raise anywhere
   inside took that local down with the frame — and the persist block below it
   never ran at all, so even the *user's own prompt* was lost. The stores were
   never the problem: both the file store and the JMFTS store are per-append
   durable, which is why the file-backed and JMFTS-backed behaviour was
   identically wrong.
2. The sequential executor ``break``\\ed on abort and synthesized nothing. That
   was invisible while an aborted turn was discarded wholesale. Once turn 1's fix
   starts persisting those turns, it starts persisting an assistant message whose
   ``tool_call_id``\\ s no result answers — a transcript a validating provider
   rejects on the next request, which turns a cancelled turn into a conversation
   that cannot be resumed.

``tau-llm/tests/test_abort_finalize.py`` covers the third: the finalizer that
turned an abort into the exception in the first place.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import patch

import pytest

from tau_agent_core.agent_loop import ABORTED_TOOL_RESULT, AgentLoop, completed_messages
from tau_agent_core.agent_loop_types import AgentLoopConfig
from tau_agent_core.agent_session import AgentSession
from tau_agent_core.session_log import InMemorySessionLog
from tau_agent_core.tools.base import AgentTool
from tau_llm.streaming import DoneEvent, ErrorEvent, TextDeltaEvent
from tau_llm.tools import ToolDefinition
from tau_llm.types import (
    AssistantMessage,
    Model,
    TextContent,
    ToolCall,
    Usage,
    UserMessage,
)

# ---------------------------------------------------------------------------
# Scaffolding
# ---------------------------------------------------------------------------


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


def _assistant_text(text: str) -> AssistantMessage:
    return AssistantMessage(
        content=[TextContent(text=text)],
        api="openai-completions",
        provider="openai",
        model="gpt-4o",
        stop_reason="stop",
        timestamp=0,
        usage=Usage(input_tokens=1, output_tokens=1, total_tokens=2),
    )


def _assistant_tool_calls(*calls: tuple[str, str, dict]) -> AssistantMessage:
    return AssistantMessage(
        content=[
            ToolCall(type="toolCall", id=cid, name=name, arguments=args)
            for cid, name, args in calls
        ],
        api="openai-completions",
        provider="openai",
        model="gpt-4o",
        stop_reason="toolUse",
        timestamp=0,
        usage=Usage(input_tokens=1, output_tokens=1, total_tokens=2),
    )


class _Stream:
    """A minimal async-iterable ``stream_simple`` result carrying fixed events."""

    def __init__(self, events: list[Any]) -> None:
        self._events = events

    def __aiter__(self):
        return self._iter()

    async def _iter(self):
        for event in self._events:
            yield event


def _tool(name: str, *, on_run=None) -> AgentTool:
    async def _execute(**kw: Any) -> str:
        if on_run is not None:
            on_run()
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


def _roles(log: InMemorySessionLog) -> list[str]:
    """The role of every message entry in the log, in order."""
    return [e["message"]["role"] for e in log.entries() if e.get("type") == "message"]


def _entries_of_role(log: InMemorySessionLog, role: str) -> list[dict]:
    return [
        e["message"]
        for e in log.entries()
        if e.get("type") == "message" and e["message"]["role"] == role
    ]


# ---------------------------------------------------------------------------
# Acceptance 1 — the completed messages reach disk
# ---------------------------------------------------------------------------


def test_a_turn_that_dies_mid_flight_still_persists_the_users_prompt():
    """The smallest statement of the bug.

    Nothing had completed except the user's own message, and that was lost too —
    because the block that persists it sat *below* ``loop.run`` with nothing
    around it. A session that swallows the thing the user typed is the part of
    this that cannot be argued as "the turn failed, so there was nothing to
    keep".
    """

    async def _boom(model, context, options=None):
        return _Stream([ErrorEvent(message="upstream 503")])

    session = _session()
    with patch("tau_agent_core.agent_loop.stream_simple", side_effect=_boom):
        with pytest.raises(RuntimeError, match="upstream 503"):
            asyncio.run(session.prompt("keep this"))

    users = _entries_of_role(session._session_log, "user")
    assert len(users) == 1
    assert users[0]["content"][0]["text"] == "keep this"


def test_a_completed_tool_result_survives_a_failure_on_the_next_turn():
    """ "Every complete message or tool result" — with a real one to lose.

    Turn 1 runs a tool to completion. Turn 2's provider errors. Before the fix
    the loop's local message list died with the frame and all of turn 1 went with
    it; the session was left holding nothing at all.
    """
    calls = {"n": 0}

    async def _fake(model, context, options=None):
        calls["n"] += 1
        if calls["n"] == 1:
            return _Stream(
                [DoneEvent(final=_assistant_tool_calls(("c1", "probe", {})), usage=Usage())]
            )
        return _Stream([ErrorEvent(message="upstream 503")])

    session = _session(tools=[_tool("probe")])
    with patch("tau_agent_core.agent_loop.stream_simple", side_effect=_fake):
        with pytest.raises(RuntimeError, match="upstream 503"):
            asyncio.run(session.prompt("go"))

    assert _roles(session._session_log) == ["user", "assistant", "toolResult"]
    result = _entries_of_role(session._session_log, "toolResult")[0]
    assert "probe ok" in str(result["content"])


def test_the_failure_still_reaches_the_caller():
    """Persisting the remains must not turn a failed turn into a quiet one.

    The exception type is preserved too, not wrapped: the loop raises several
    (``RuntimeError`` from a provider error, ``ValueError`` from a malformed tool
    call, ``CancelledError`` from a hard cancel) and a caller that catches one of
    them specifically must keep working.
    """

    async def _boom(model, context, options=None):
        return _Stream([ErrorEvent(message="upstream 503")])

    session = _session()
    with patch("tau_agent_core.agent_loop.stream_simple", side_effect=_boom):
        with pytest.raises(RuntimeError) as excinfo:
            asyncio.run(session.prompt("hello"))
    assert type(excinfo.value) is RuntimeError
    assert "upstream 503" in str(excinfo.value)


def test_a_silent_submission_still_writes_nothing_when_it_fails():
    """``persist=False`` is not weakened by the new failure path.

    A ``store_history=False`` submission runs the real loop and writes none of
    it. The failure path takes the same ``persist`` flag as the success path, so
    a turn that dies is not a way to get a silent submission recorded.
    """

    async def _boom(model, context, options=None):
        return _Stream([ErrorEvent(message="upstream 503")])

    session = _session()
    with patch("tau_agent_core.agent_loop.stream_simple", side_effect=_boom):
        with pytest.raises(RuntimeError):
            asyncio.run(session._run_one_turn("quiet", None, None, persist=False))

    # Messages only. The ``agent_spec`` entry is written when the session records
    # what model/prompt the turn ran under, and it does not consult ``persist`` —
    # pre-existing behaviour, unrelated to this path, and not what the flag is
    # about.
    assert _roles(session._session_log) == []


def test_the_completed_messages_ride_the_exception():
    """The seam itself, asserted directly at the loop boundary.

    ``AgentSession`` is one caller. Anything else driving ``AgentLoop.run`` needs
    the same access to what finished, and an attribute on the exception is how it
    gets it without the loop having to know who is catching.
    """
    calls = {"n": 0}

    async def _fake(model, context, options=None):
        calls["n"] += 1
        if calls["n"] == 1:
            return _Stream(
                [DoneEvent(final=_assistant_tool_calls(("c1", "probe", {})), usage=Usage())]
            )
        return _Stream([ErrorEvent(message="upstream 503")])

    loop = AgentLoop(config=AgentLoopConfig(model="gpt-4o"), tools=[_tool("probe")])

    async def _go():
        with patch("tau_agent_core.agent_loop.stream_simple", side_effect=_fake):
            with pytest.raises(RuntimeError) as excinfo:
                await loop.run(prompts=[UserMessage(content=[TextContent(text="go")], timestamp=0)])
            return completed_messages(excinfo.value)

    finished = asyncio.run(_go())
    assert [getattr(m, "role", None) or m.get("role") for m in finished] == [
        "assistant",
        "toolResult",
    ]


def test_completed_messages_is_empty_for_an_unrelated_exception():
    """The reader asks unconditionally, so it must answer for any exception."""
    assert completed_messages(ValueError("nothing to do with the loop")) == []


# ---------------------------------------------------------------------------
# Acceptance 2 — every tool_call_id is answered
# ---------------------------------------------------------------------------


def test_an_abort_before_the_batch_answers_every_call():
    """Esc landed while the assistant message was still streaming.

    Nothing may execute — and every call the message asked for must still get a
    result, or the persisted transcript carries ``tool_call_id``\\ s that nothing
    answers.
    """
    ran: list[str] = []

    async def _fake(model, context, options=None):
        signal = options.get("abort_signal") if options else None
        if signal is not None:
            signal.abort()
        return _Stream(
            [
                DoneEvent(
                    final=_assistant_tool_calls(("c1", "probe", {}), ("c2", "probe", {})),
                    usage=Usage(),
                )
            ]
        )

    session = _session(tools=[_tool("probe", on_run=lambda: ran.append("probe"))])
    with patch("tau_agent_core.agent_loop.stream_simple", side_effect=_fake):
        asyncio.run(session.prompt("go"))

    assert ran == [], "a tool ran after the user aborted"
    results = _entries_of_role(session._session_log, "toolResult")
    answered = {r["tool_call_id"] for r in results}
    assert answered == {"c1", "c2"}
    for r in results:
        assert ABORTED_TOOL_RESULT in str(r["content"])


def test_an_abort_mid_batch_keeps_the_results_it_already_had():
    """Half a batch ran. Those results are real and are kept as themselves.

    Only the calls with no result yet are answered as aborted — otherwise
    "answer everything outstanding" would overwrite work the user did get.
    """
    ran: list[str] = []
    # A one-slot holder because the tool has to abort the session that is running
    # it, and the session cannot be constructed until its tools exist.
    holder: dict[str, AgentSession] = {}

    def _abort_after_first() -> None:
        ran.append("first")
        holder["session"].abort()

    async def _fake(model, context, options=None):
        return _Stream(
            [
                DoneEvent(
                    final=_assistant_tool_calls(
                        ("c1", "first", {}),
                        ("c2", "second", {}),
                        ("c3", "second", {}),
                    ),
                    usage=Usage(),
                )
            ]
        )

    session = _session(
        tools=[_tool("first", on_run=_abort_after_first), _tool("second")],
        tool_execution_mode="sequential",
    )
    holder["session"] = session

    with patch("tau_agent_core.agent_loop.stream_simple", side_effect=_fake):
        asyncio.run(session.prompt("go"))

    assert ran == ["first"], "the batch continued past the abort"
    results = {
        r["tool_call_id"]: str(r["content"])
        for r in _entries_of_role(session._session_log, "toolResult")
    }
    assert set(results) == {"c1", "c2", "c3"}
    assert "first ok" in results["c1"], "a completed result was overwritten by the abort"
    assert ABORTED_TOOL_RESULT in results["c2"]
    assert ABORTED_TOOL_RESULT in results["c3"]


def test_a_parallel_batch_is_stopped_by_an_abort_too():
    """The parallel executor has no abort check of its own.

    It gathers every prepared call at once, so without a guard ABOVE it an abort
    that landed during streaming would still run the whole batch. The guard is in
    ``_execute_tool_calls``, which is the one place both modes pass through.
    """
    ran: list[str] = []

    async def _fake(model, context, options=None):
        signal = options.get("abort_signal") if options else None
        if signal is not None:
            signal.abort()
        return _Stream(
            [
                DoneEvent(
                    final=_assistant_tool_calls(("c1", "probe", {}), ("c2", "probe", {})),
                    usage=Usage(),
                )
            ]
        )

    session = _session(
        tools=[_tool("probe", on_run=lambda: ran.append("probe"))],
        tool_execution_mode="parallel",
    )
    with patch("tau_agent_core.agent_loop.stream_simple", side_effect=_fake):
        asyncio.run(session.prompt("go"))

    assert ran == []
    assert {r["tool_call_id"] for r in _entries_of_role(session._session_log, "toolResult")} == {
        "c1",
        "c2",
    }


def test_an_aborted_call_still_emits_a_start_and_an_end():
    """A synthesized result needs a widget to land in.

    A front-end folds a ``tool_execution_end`` into the box its matching
    ``tool_execution_start`` created, so an end with no start is silently dropped
    and the user sees a turn that just stops. This is the same rule the veto path
    already states.
    """
    events: list[Any] = []

    async def _fake(model, context, options=None):
        signal = options.get("abort_signal") if options else None
        if signal is not None:
            signal.abort()
        return _Stream([DoneEvent(final=_assistant_tool_calls(("c1", "probe", {})), usage=Usage())])

    loop = AgentLoop(
        config=AgentLoopConfig(model="gpt-4o"),
        emit=lambda e: _record(events, e),
        tools=[_tool("probe")],
        abort_signal=_TrippableSignal(),
    )

    async def _go():
        with patch("tau_agent_core.agent_loop.stream_simple", side_effect=_fake):
            await loop.run(prompts=[UserMessage(content=[TextContent(text="go")], timestamp=0)])

    asyncio.run(_go())

    kinds = [(e.type, e.tool_call_id) for e in events if e.type.startswith("tool_execution_")]
    assert kinds == [("tool_execution_start", "c1"), ("tool_execution_end", "c1")]


async def _record(sink: list[Any], event: Any) -> None:
    sink.append(event)


class _TrippableSignal:
    """The loop's abort signal, tripped by the stream double via ``options``."""

    def __init__(self) -> None:
        self._aborted = False

    def abort(self, reason: str | None = None) -> None:
        self._aborted = True

    def is_aborted(self) -> bool:
        return self._aborted


# ---------------------------------------------------------------------------
# The clean path is untouched
# ---------------------------------------------------------------------------


def test_an_ordinary_turn_persists_exactly_what_it_always_did():
    """The success path was refactored (the persist block moved into a helper).

    Its output must be byte-for-byte what it was, in the same order, or every
    reload forks: the next load rebuilds the path from these entries and it has
    to be the path the model saw.
    """

    async def _fake(model, context, options=None):
        return _Stream(
            [
                TextDeltaEvent(delta="hi", partial=_assistant_text("hi")),
                DoneEvent(final=_assistant_text("hi"), usage=Usage()),
            ]
        )

    session = _session()
    with patch("tau_agent_core.agent_loop.stream_simple", side_effect=_fake):
        asyncio.run(session.prompt("hello"))

    assert _roles(session._session_log) == ["user", "assistant"]

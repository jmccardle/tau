"""Side work announces itself: the three ``side_completion_*`` events.

Reference: docs/STREAMING-SIDE-WORK.md

A compaction and a branch summary spend tokens and produce text outside any
turn. Before this vocabulary existed a head could only show a spinner, and the
tokens were invisible everywhere — no ``turn_end`` counts them. These tests hold
the three properties a renderer depends on: the fragments arrive while the work
runs, exactly one end arrives however it ended, and the end says what it cost.
"""

from __future__ import annotations

from typing import Any

from tau_agent_core.agent_session import AgentSession
from tau_agent_core.compaction import CompactionError, CompactionSettings
from tau_agent_core.events import AgentEvent
from tau_agent_core.session_log import InMemorySessionLog
from tau_llm.types import AssistantMessage, Model, TextContent, Usage

#: A fixed epoch-ms stamp for fixtures — never 0 (docs/MESSAGE-TIMESTAMPS.md §2).
_TS = 1_700_000_000_000

#: Split across three deltas so "the fragments arrive separately" is testable.
_SUMMARY_PIECES = ("the ", "whole ", "summary")


def _model() -> Model:
    return Model(
        id="summarizer-m",
        provider="openai",
        api="openai-completions",
        base_url="http://127.0.0.1:1/v1",
        name="summarizer-m",
        context_window=8192,
        max_tokens=256,
    )


def _reply(text: str) -> AssistantMessage:
    return AssistantMessage(
        role="assistant",
        content=[TextContent(type="text", text=text)],
        api="openai-completions",
        provider="openai",
        model="summarizer-m",
        stop_reason="stop",
        timestamp=_TS,
        usage=Usage(input_tokens=4000, output_tokens=120, total_tokens=4120),
    )


def _session() -> AgentSession:
    return AgentSession(
        session_log=InMemorySessionLog(),
        model=_model(),
        system_prompt="",
        tools=[],
        api_key="k",
        # The shipped 20k would need a 20k-token fixture to reach a cut point.
        compaction_settings=CompactionSettings(reserve_tokens=256, keep_recent_tokens=200),
    )


async def _seed(session: AgentSession, turns: int = 3) -> None:
    """Enough conversation that ``prepare_compaction`` finds a cut point."""
    for i in range(turns):
        await session.session_log.append_message(
            {"role": "user", "content": [{"type": "text", "text": f"question {i} " + "x" * 400}]}
        )
        await session.session_log.append_message(
            {"role": "assistant", "content": [{"type": "text", "text": f"answer {i} " + "y" * 400}]}
        )


def _streaming_summarizer(pieces: tuple[str, ...] = _SUMMARY_PIECES):
    """A ``complete_simple`` double that actually feeds the sink, like the real one."""

    async def _impl(model, context, options=None, *, on_text_delta=None, **_: Any):
        for piece in pieces:
            if on_text_delta is not None:
                sunk = on_text_delta(piece)
                if sunk is not None:
                    await sunk
        return _reply("".join(pieces))

    return _impl


def _collect(session: AgentSession) -> list[AgentEvent]:
    seen: list[AgentEvent] = []

    async def _on(event: AgentEvent) -> None:
        if event.type.startswith("side_completion_"):
            seen.append(event)

    session.subscribe(_on)
    return seen


async def test_a_compaction_streams_its_summary_and_reports_what_it_spent(monkeypatch):
    monkeypatch.setattr("tau_agent_core.compaction.complete_simple", _streaming_summarizer())
    session = _session()
    await _seed(session)
    seen = _collect(session)

    await session.compact()

    assert [e.type for e in seen] == [
        "side_completion_start",
        "side_completion_update",
        "side_completion_update",
        "side_completion_update",
        "side_completion_end",
    ]
    assert all(e.purpose == "compaction" for e in seen)
    assert [e.delta for e in seen[1:-1]] == list(_SUMMARY_PIECES)

    end = seen[-1]
    assert end.is_error is False
    assert end.text is not None and "".join(_SUMMARY_PIECES) in end.text
    assert end.usage is not None
    assert end.usage["input_tokens"] == 4000
    assert end.usage["output_tokens"] == 120


async def test_a_manual_compaction_and_the_auto_trigger_are_distinguishable(monkeypatch):
    """`reason` is the one thing the two compaction callers differ by on the wire.

    Without it a reader watching a summary arrive could not tell one they asked
    for from one `_maybe_auto_compact` imposed — and the imposed one is the one
    they are more likely to be surprised by. Set on all three events, so a reader
    that attached mid-summary still learns it.
    """
    monkeypatch.setattr("tau_agent_core.compaction.complete_simple", _streaming_summarizer())

    manual = _session()
    await _seed(manual)
    seen_manual = _collect(manual)
    await manual.compact()
    assert {e.reason for e in seen_manual} == {"manual"}
    assert len(seen_manual) == 5, "reason rides every event, not just the start"

    auto = _session()
    await _seed(auto)
    seen_auto = _collect(auto)
    await auto._perform_compaction("threshold")
    assert {e.reason for e in seen_auto} == {"threshold"}


async def test_the_start_names_the_summarizer_not_the_conversations_model(monkeypatch):
    """The summariser is often a cheaper model, and the reader is paying for IT."""
    monkeypatch.setattr("tau_agent_core.compaction.complete_simple", _streaming_summarizer())
    session = _session()
    await _seed(session)
    seen = _collect(session)

    await session.compact()

    assert (seen[0].message or {}).get("model") == "summarizer-m"


async def test_a_failed_compaction_still_ends_its_own_bracket(monkeypatch):
    """Otherwise a renderer holds an open box for the life of the session."""

    async def _boom(model, context, options=None, **_: Any):
        raise RuntimeError("the summarizer fell over")

    monkeypatch.setattr("tau_agent_core.compaction.complete_simple", _boom)
    session = _session()
    await _seed(session)
    seen = _collect(session)

    try:
        await session.compact()
    except CompactionError:
        pass

    assert [e.type for e in seen] == ["side_completion_start", "side_completion_end"]
    end = seen[-1]
    assert end.is_error is True
    assert end.text is None, "a failed summary must not present a partial one as whole"
    assert "the summarizer fell over" in (end.error or "")


async def test_nothing_to_compact_announces_nothing(monkeypatch):
    """A start with no end is worse than silence, so the no-op emits neither."""
    monkeypatch.setattr("tau_agent_core.compaction.complete_simple", _streaming_summarizer())
    session = _session()
    seen = _collect(session)

    assert await session.compact() is None
    assert seen == []


async def test_the_summary_the_end_carries_is_what_was_written_to_the_log(monkeypatch):
    """The fragments are a PREFIX: compaction stitches the file lists on afterwards.

    So a renderer that kept only the deltas would show a summary the log does not
    hold. The end event carries the finished text for exactly this reason.
    """
    monkeypatch.setattr("tau_agent_core.compaction.complete_simple", _streaming_summarizer())
    session = _session()
    await _seed(session)
    seen = _collect(session)

    await session.compact()

    entries = [e for e in session.session_log.entries() if e.get("type") == "compaction"]
    assert len(entries) == 1
    assert seen[-1].text == entries[0]["summary"]

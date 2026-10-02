"""Two-limit automatic compaction, end to end against a faked network boundary.

The behaviour under test is the difference between the two limits. A **soft**
crossing can wait for the turn to finish, because the turn is over. A **hard**
crossing cannot: the loop is about to send another request in the same turn, and
that is the request the provider refuses. Before this, τ only had the soft check,
so a tool-heavy turn could blow the window twenty calls in and lose the turn.

Only ``stream_simple`` (the agent loop's wire) and ``complete_simple`` (the
summarizer's) are faked. The session, the loop, the tree, the cut-point search
and the persistence are all real, because the thing most likely to be wrong is
the ordering between them: a mid-turn compaction has to flush the turn so far
BEFORE it cuts, and must not write those messages a second time at the tail.

Reference: docs/TOKEN-ACCOUNTING.md
"""

from __future__ import annotations

import math
from typing import Any
from unittest.mock import patch

import pytest

from tau_llm.streaming import DoneEvent
from tau_llm.types import (
    AssistantMessage,
    Model,
    TextContent,
    ToolCall,
    Usage,
)

from tau_agent_core.agent_session import AgentSession, _newest_compaction_ms
from tau_agent_core.compaction import (
    CompactionSettings,
    compaction_limits,
    must_compact,
    should_compact,
    try_compaction_limits,
)
from tau_agent_core.cursor import Cursor
from tau_agent_core.session_log import InMemorySessionLog
from tau_agent_core.tools.base import AgentTool, AgentToolResult, ToolDefinition

_TS = 1_700_000_000_000


def _model(window: int = 40_000) -> Model:
    return Model(
        id="test-model",
        name="Test",
        api="openai-completions",
        provider="openai",
        base_url="http://example.invalid/v1",
        context_window=window,
        max_tokens=4096,
    )


def _msg(role: str, text: str) -> dict:
    return {"role": role, "content": [{"type": "text", "text": text}]}


class _Stream:
    """Minimal async stream matching the ``stream_simple`` contract."""

    def __init__(self, final: AssistantMessage) -> None:
        self._events = [DoneEvent(final=final, usage=final.usage or Usage())]

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
        return self._events[-1].final

    def abort(self) -> None:
        pass


def _tool_call(call_id: str, size: int) -> AssistantMessage:
    return AssistantMessage(
        content=[ToolCall(type="toolCall", id=call_id, name="bulk", arguments={"size": size})],
        api="openai-completions",
        provider="openai",
        model="test-model",
        stop_reason="toolUse",
        timestamp=_TS,
        usage=Usage(),
    )


def _text(text: str, *, billed: int = 0) -> AssistantMessage:
    usage = Usage()
    if billed:
        usage = Usage(input_tokens=billed, output_tokens=10, total_tokens=billed + 10)
    return AssistantMessage(
        content=[TextContent(text=text)],
        api="openai-completions",
        provider="openai",
        model="test-model",
        stop_reason="stop",
        timestamp=_TS,
        usage=usage,
    )


async def _bulk(tool_call_id: str, args: dict[str, Any], signal: Any = None) -> AgentToolResult:
    """Return ``size`` characters, so a turn can be made to grow on demand."""
    return AgentToolResult(
        tool_name="bulk",
        tool_call_id=tool_call_id,
        content=[{"type": "text", "text": "lorem ipsum " * (int(args["size"]) // 12)}],
    )


def _bulk_tool() -> AgentTool:
    return AgentTool(
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


async def _summary(model, context, options=None, **_):
    return AssistantMessage(
        content=[TextContent(text="## Goal\nCOMPACTED-SUMMARY")],
        api="openai-completions",
        provider="openai",
        model="test-model",
        stop_reason="stop",
        timestamp=_TS,
        usage=Usage(input_tokens=500, output_tokens=50, total_tokens=550),
    )


# ── the limits themselves ────────────────────────────────────────────────────


def test_default_limits_are_derived_from_the_window_and_ordered() -> None:
    limits = compaction_limits(128_000, CompactionSettings())
    assert limits.hard == 128_000 - 16384
    assert limits.soft == math.floor(limits.hard * 0.8)
    assert limits.soft < limits.hard <= limits.window


def test_pinned_limits_override_the_window() -> None:
    settings = CompactionSettings(hard_limit_tokens=20_000, soft_limit_tokens=16_000)
    limits = compaction_limits(172_032, settings)
    assert (limits.soft, limits.hard) == (16_000, 20_000)


def test_a_soft_limit_above_the_hard_one_is_refused() -> None:
    settings = CompactionSettings(hard_limit_tokens=1000, soft_limit_tokens=5000)
    with pytest.raises(ValueError, match="soft limit 5000 is above hard limit 1000"):
        compaction_limits(128_000, settings)


def test_a_hard_limit_beyond_the_window_is_refused() -> None:
    settings = CompactionSettings(hard_limit_tokens=200_000, soft_limit_tokens=1000)
    with pytest.raises(ValueError, match="exceeds context_window"):
        compaction_limits(128_000, settings)


def test_a_window_smaller_than_its_margins_is_refused() -> None:
    with pytest.raises(ValueError, match="too small for that margin"):
        compaction_limits(8192, CompactionSettings())


def test_a_window_smaller_than_its_margins_simply_never_compacts() -> None:
    """Not a swallowed error: there is no threshold such a model could compact at."""
    settings = CompactionSettings()
    assert try_compaction_limits(8192, settings) is None
    assert should_compact(8_000_000, 8192, settings) is False
    assert must_compact(8_000_000, 8192, settings) is False


def test_the_two_predicates_fire_in_the_right_order() -> None:
    settings = CompactionSettings(hard_limit_tokens=20_000, soft_limit_tokens=16_000)
    window = 40_000
    assert (should_compact(15_000, window, settings), must_compact(15_000, window, settings)) == (
        False,
        False,
    )
    assert (should_compact(18_000, window, settings), must_compact(18_000, window, settings)) == (
        True,
        False,
    )
    assert (should_compact(25_000, window, settings), must_compact(25_000, window, settings)) == (
        True,
        True,
    )


def test_disabled_settings_never_fire_either_check() -> None:
    settings = CompactionSettings(enabled=False, hard_limit_tokens=100, soft_limit_tokens=50)
    assert should_compact(10_000, 40_000, settings) is False
    assert must_compact(10_000, 40_000, settings) is False


# ── the session's own reading ────────────────────────────────────────────────


def _session(settings: CompactionSettings, window: int = 40_000) -> AgentSession:
    return AgentSession(
        session_log=InMemorySessionLog(),
        model=_model(window),
        compaction_settings=settings,
        tools=[_bulk_tool()],
    )


async def test_context_estimate_anchors_on_the_provider_and_labels_itself() -> None:
    session = _session(CompactionSettings())
    await session.cursor.append_message(_msg("user", "hello"))
    await session.cursor.append_message(
        {
            **_msg("assistant", "hi"),
            "usage": {"input_tokens": 9000, "output_tokens": 100, "total_tokens": 9100},
            "timestamp": _TS,
        }
    )
    await session.cursor.append_message(_msg("user", "x" * 4000))

    estimate = session.context_estimate()
    assert estimate.usage_tokens == 9100
    assert estimate.trailing_tokens > 0
    assert estimate.tokens == estimate.usage_tokens + estimate.trailing_tokens
    assert estimate.count.exact is False
    assert estimate.count.source == "classes"


async def test_the_calibrator_learns_from_billed_turns() -> None:
    session = _session(CompactionSettings())
    for i in range(5):
        await session.cursor.append_message({**_msg("user", "u" * 400), "timestamp": _TS + i})
        await session.cursor.append_message(
            {
                **_msg("assistant", "a" * 200),
                "usage": {
                    "input_tokens": 2000 * (i + 1),
                    "output_tokens": 50,
                    "total_tokens": 2000 * (i + 1) + 50,
                },
                "timestamp": _TS + 1000 + i,
            }
        )
        session.context_estimate()

    assert session._calibrator.observations == 5


async def test_a_turn_is_observed_at_most_once() -> None:
    session = _session(CompactionSettings())
    await session.cursor.append_message({**_msg("user", "u"), "timestamp": _TS})
    await session.cursor.append_message(
        {
            **_msg("assistant", "a"),
            "usage": {"input_tokens": 1000, "output_tokens": 10, "total_tokens": 1010},
            "timestamp": _TS + 1,
        }
    )
    for _ in range(5):
        session.context_estimate()
    assert session._calibrator.observations == 1


# ── mid-turn (hard limit) ────────────────────────────────────────────────────


async def test_a_long_turn_compacts_mid_turn_and_keeps_going(monkeypatch) -> None:
    """The case the soft check cannot reach: the window fills inside one turn."""
    monkeypatch.setattr("tau_agent_core.compaction.complete_simple", _summary)
    settings = CompactionSettings(
        hard_limit_tokens=6_000,
        soft_limit_tokens=5_000,
        keep_recent_tokens=800,
        reserve_tokens=1_000,
    )
    session = _session(settings)

    responses = [_tool_call(f"c{i}", 9000) for i in range(6)] + [_text("done", billed=1200)]
    sent_sizes: list[int] = []

    def fake(model, context, options=None, **kwargs):
        sent_sizes.append(len(context.get("messages", [])))
        return _Stream(responses[min(len(sent_sizes) - 1, len(responses) - 1)])

    with patch("tau_agent_core.agent_loop.stream_simple", side_effect=fake):
        await session.prompt("start")

    entries = session.session_log.entries()
    compactions = [e for e in entries if e["type"] == "compaction"]
    assert compactions, "the hard limit was crossed mid-turn and nothing compacted"

    # The loop kept running past the compaction rather than ending the turn.
    assert len(sent_sizes) == len(responses)
    # And the request AFTER a compaction is smaller than the one before it.
    assert min(sent_sizes[1:]) < max(sent_sizes)


async def test_mid_turn_compaction_writes_each_message_exactly_once(monkeypatch) -> None:
    monkeypatch.setattr("tau_agent_core.compaction.complete_simple", _summary)
    settings = CompactionSettings(
        hard_limit_tokens=6_000,
        soft_limit_tokens=5_000,
        keep_recent_tokens=800,
        reserve_tokens=1_000,
    )
    session = _session(settings)

    responses = [_tool_call(f"c{i}", 9000) for i in range(4)] + [_text("done", billed=900)]
    calls = {"n": 0}

    def fake(model, context, options=None, **kwargs):
        i = calls["n"]
        calls["n"] += 1
        return _Stream(responses[min(i, len(responses) - 1)])

    with patch("tau_agent_core.agent_loop.stream_simple", side_effect=fake):
        returned = await session.prompt("start")

    entries = [e for e in session.session_log.entries() if e["type"] == "message"]
    ids = [e["id"] for e in entries]
    assert len(ids) == len(set(ids))

    tool_results = [e for e in entries if (e.get("message") or {}).get("role") == "toolResult"]
    assert len(tool_results) == 4, "one tool result per call, written once each"

    users = [e for e in entries if (e.get("message") or {}).get("role") == "user"]
    assert len(users) == 1, "the user message must not be written twice"

    returned_roles = [m.get("role") for m in returned]
    assert returned_roles.count("user") == 1
    assert returned_roles.count("toolResult") == 4


async def test_the_compacted_path_survives_a_reload(monkeypatch) -> None:
    monkeypatch.setattr("tau_agent_core.compaction.complete_simple", _summary)
    settings = CompactionSettings(
        hard_limit_tokens=6_000,
        soft_limit_tokens=5_000,
        keep_recent_tokens=800,
        reserve_tokens=1_000,
    )
    session = _session(settings)
    responses = [_tool_call(f"c{i}", 9000) for i in range(4)] + [_text("done", billed=900)]
    calls = {"n": 0}

    def fake(model, context, options=None, **kwargs):
        i = calls["n"]
        calls["n"] += 1
        return _Stream(responses[min(i, len(responses) - 1)])

    with patch("tau_agent_core.agent_loop.stream_simple", side_effect=fake):
        await session.prompt("start")

    log = session.session_log
    reloaded = Cursor.newest(log).context()
    text = "".join(
        block.get("text", "")
        for m in reloaded
        if isinstance(m, dict)
        for block in (m.get("content") or [])
        if isinstance(block, dict)
    )
    assert "COMPACTED-SUMMARY" in text


async def test_a_short_turn_never_triggers_the_mid_turn_check(monkeypatch) -> None:
    monkeypatch.setattr("tau_agent_core.compaction.complete_simple", _summary)
    session = _session(CompactionSettings(hard_limit_tokens=30_000, soft_limit_tokens=25_000))
    responses = [_tool_call("c0", 40), _text("done", billed=300)]
    calls = {"n": 0}

    def fake(model, context, options=None, **kwargs):
        i = calls["n"]
        calls["n"] += 1
        return _Stream(responses[min(i, len(responses) - 1)])

    with patch("tau_agent_core.agent_loop.stream_simple", side_effect=fake):
        await session.prompt("hi")

    assert not [e for e in session.session_log.entries() if e["type"] == "compaction"]


async def test_disabling_compaction_leaves_a_long_turn_alone(monkeypatch) -> None:
    """Fail-Early's opposite number: opting out must actually opt out."""
    monkeypatch.setattr("tau_agent_core.compaction.complete_simple", _summary)
    settings = CompactionSettings(
        enabled=False, hard_limit_tokens=6_000, soft_limit_tokens=5_000, reserve_tokens=1_000
    )
    session = _session(settings)
    responses = [_tool_call(f"c{i}", 9000) for i in range(4)] + [_text("done", billed=900)]
    calls = {"n": 0}

    def fake(model, context, options=None, **kwargs):
        i = calls["n"]
        calls["n"] += 1
        return _Stream(responses[min(i, len(responses) - 1)])

    with patch("tau_agent_core.agent_loop.stream_simple", side_effect=fake):
        await session.prompt("start")

    assert not [e for e in session.session_log.entries() if e["type"] == "compaction"]


# ── end of turn (soft limit) ─────────────────────────────────────────────────


async def test_crossing_only_the_soft_limit_compacts_after_the_turn(monkeypatch) -> None:
    monkeypatch.setattr("tau_agent_core.compaction.complete_simple", _summary)
    settings = CompactionSettings(
        hard_limit_tokens=30_000,
        soft_limit_tokens=5_000,
        keep_recent_tokens=800,
        reserve_tokens=1_000,
    )
    session = _session(settings)
    for i in range(4):
        await session.cursor.append_message({**_msg("user", f"u{i}"), "timestamp": _TS + i})
        await session.cursor.append_message(
            {**_msg("assistant", f"a{i}"), "timestamp": _TS + 100 + i}
        )

    responses = [_text("done", billed=9_000)]
    order: list[str] = []

    def fake(model, context, options=None, **kwargs):
        order.append("request")
        return _Stream(responses[0])

    real_perform = AgentSession._perform_compaction

    async def traced(self, reason, custom_instructions=None):
        order.append(f"compact:{reason}")
        return await real_perform(self, reason, custom_instructions=custom_instructions)

    with (
        patch("tau_agent_core.agent_loop.stream_simple", side_effect=fake),
        patch.object(AgentSession, "_perform_compaction", traced),
    ):
        await session.prompt("go")

    assert order == ["request", "compact:threshold"], order


# ── two defects this work surfaced ───────────────────────────────────────────


def test_a_zero_usage_report_is_not_an_anchor() -> None:
    """A provider that reported nothing is silence, not a zero-token context.

    Anchoring on an all-zero ``usage`` priced every message before it at zero, so
    a conversation whose newest turn reported no usage read as just its tail.
    """
    from tau_agent_core.compaction import estimate_context_tokens

    messages = [
        _msg("user", "u" * 4000),
        {**_msg("assistant", "a"), "usage": {"input_tokens": 0, "output_tokens": 0}},
    ]
    estimate = estimate_context_tokens(messages)
    assert estimate.last_usage_index is None
    assert estimate.tokens > 100, "the 4000-character user message must still be counted"


def test_a_cut_inside_the_newest_turn_cuts_late_not_first() -> None:
    """The retention target met inside one turn must not cut back to the start.

    ``find_cut_point`` walks back from the end accumulating tokens, then takes the
    first cut point at or after where it stopped. When the newest message alone
    meets the target there IS no such cut point, and falling back to
    ``cut_points[0]`` chose the EARLIEST — which keeps the whole conversation and
    makes ``prepare_compaction`` return None. A single turn larger than
    ``keep_recent_tokens`` could then never be compacted at all.
    """
    from tau_agent_core.compaction import find_cut_point

    entries = [
        {"id": "e0", "type": "message", "message": _msg("user", "start")},
        {"id": "e1", "type": "message", "message": _msg("assistant", "calling")},
        {
            "id": "e2",
            "type": "message",
            "message": {
                "role": "toolResult",
                "tool_call_id": "c0",
                "tool_name": "bulk",
                "content": [{"type": "text", "text": "lorem ipsum " * 5000}],
            },
        },
    ]
    cut = find_cut_point(entries, 0, len(entries), keep_recent_tokens=800)
    assert cut.first_kept_entry_index == 1, "cut at the newest cut point, not the first"
    assert cut.is_split_turn is True
    assert cut.turn_start_index == 0


def test_a_single_overlong_turn_is_preparable() -> None:
    """The consequence of the fix above, at the level a caller sees."""
    from tau_agent_core.compaction import prepare_compaction

    entries = [
        {"id": "e0", "type": "message", "message": _msg("user", "start")},
        {"id": "e1", "type": "message", "message": _msg("assistant", "calling")},
        {
            "id": "e2",
            "type": "message",
            "message": {
                "role": "toolResult",
                "tool_call_id": "c0",
                "tool_name": "bulk",
                "content": [{"type": "text", "text": "lorem ipsum " * 5000}],
            },
        },
    ]
    prep = prepare_compaction(entries, CompactionSettings(keep_recent_tokens=800))
    assert prep is not None
    assert prep.is_split_turn is True
    assert prep.turn_prefix_messages, "the turn's prefix is what there is to summarize"


async def test_the_estimate_tracks_the_turn_while_the_turn_is_running(monkeypatch) -> None:
    """The persisted path is stale mid-turn, and the header reads the same number.

    Nothing a turn produces reaches the session log until the turn ends, so a
    bare `context_estimate()` during a long tool loop reported the conversation
    as it stood before the turn began. The loop publishes its live context at
    each turn boundary; this asserts the reading climbs with it.
    """
    monkeypatch.setattr("tau_agent_core.compaction.complete_simple", _summary)
    session = _session(CompactionSettings(hard_limit_tokens=200_000, soft_limit_tokens=150_000))
    readings: list[int] = []
    responses = [_tool_call(f"c{i}", 6000) for i in range(3)] + [_text("done", billed=400)]
    calls = {"n": 0}

    def fake(model, context, options=None, **kwargs):
        readings.append(session.context_estimate().tokens)
        i = calls["n"]
        calls["n"] += 1
        return _Stream(responses[min(i, len(responses) - 1)])

    with patch("tau_agent_core.agent_loop.stream_simple", side_effect=fake):
        await session.prompt("start")

    assert readings[0] == 0, "nothing is persisted before the first request of a turn"
    assert len(readings) == 4
    assert readings[1] > 0, "the loop's own context reaches the estimate at the first boundary"
    assert readings[1] < readings[2] < readings[3], "and it climbs as tool results arrive"


def test_the_usage_anchor_does_not_survive_a_compaction() -> None:
    """A turn billed before a compaction describes a context that is now gone.

    Measured live 2026-09-18: the first request after a mid-turn compaction read
    22,690 tokens against a billed 5,734 — ratio 3.96, back over the hard limit
    that had just fired. Anchoring past a compaction summary is a compaction
    loop, not merely a wrong display.
    """
    from tau_agent_core.compaction import _summary_context_message, estimate_context_tokens

    path = [
        _msg("user", "old question"),
        {
            **_msg("assistant", "old answer"),
            "usage": {"input_tokens": 18000, "output_tokens": 300, "total_tokens": 18300},
        },
        _summary_context_message("## Goal\nwhat came before"),
        _msg("user", "next question"),
    ]
    estimate = estimate_context_tokens(path)
    assert estimate.last_usage_index is None, "the pre-compaction anchor must not be used"
    assert estimate.usage_tokens == 0
    assert estimate.tokens < 18300, "the folded-away conversation is not in the context"


def test_an_anchor_after_the_compaction_is_used_normally() -> None:
    """The stop is at the compaction, not at every summary-looking message."""
    from tau_agent_core.compaction import _summary_context_message, estimate_context_tokens

    path = [
        _summary_context_message("earlier work"),
        _msg("user", "next question"),
        {
            **_msg("assistant", "fresh answer"),
            "usage": {"input_tokens": 900, "output_tokens": 50, "total_tokens": 950},
        },
        _msg("user", "another"),
    ]
    estimate = estimate_context_tokens(path)
    assert estimate.last_usage_index == 2
    assert estimate.usage_tokens == 950


async def test_the_reading_drops_after_a_mid_turn_compaction(monkeypatch) -> None:
    """The point of compacting is that the number goes down, and stays down.

    Two things had to be right for this, and each was wrong on its own first:
    the usage anchor must not survive the cut, and the live context the loop
    publishes must be REPLACED by the compacted one rather than left holding the
    path that was just folded away. Measured live, the second fault alone kept
    the reading at 22,586 against a billed 5,734.
    """
    monkeypatch.setattr("tau_agent_core.compaction.complete_simple", _summary)
    settings = CompactionSettings(
        hard_limit_tokens=6_000,
        soft_limit_tokens=5_000,
        keep_recent_tokens=800,
        reserve_tokens=1_000,
    )
    session = _session(settings)
    readings: list[int] = []
    responses = [_tool_call(f"c{i}", 9000) for i in range(5)] + [_text("done", billed=900)]
    calls = {"n": 0}

    def fake(model, context, options=None, **kwargs):
        readings.append(session.context_estimate().tokens)
        i = calls["n"]
        calls["n"] += 1
        return _Stream(responses[min(i, len(responses) - 1)])

    with patch("tau_agent_core.agent_loop.stream_simple", side_effect=fake):
        await session.prompt("start")

    assert [e for e in session.session_log.entries() if e["type"] == "compaction"]
    peak = max(readings)
    after = readings[readings.index(peak) + 1 :]
    assert after, "the loop must continue past the compaction"
    assert min(after) < peak, f"the reading never fell after compacting: {readings}"


def test_a_kept_tails_usage_is_disqualified_by_the_compaction_that_kept_it() -> None:
    """Position cannot catch this one, which is why the boundary is wall-clock.

    A compaction keeps a recent tail. That tail's assistant turn was billed
    against the whole pre-fold conversation, and it sits AFTER the summary in the
    flattened path — so walking backwards and stopping at the summary finds the
    stale anchor first. Measured live: 22,588 predicted against 5,734 billed.
    """
    from tau_agent_core.compaction import _summary_context_message, estimate_context_tokens

    fold_at = _TS + 5_000
    path = [
        _summary_context_message("## Goal\nthe folded conversation"),
        {
            **_msg("assistant", "answer from before the fold"),
            "usage": {"input_tokens": 18000, "output_tokens": 300, "total_tokens": 18300},
            "timestamp": _TS,
        },
        {
            **_msg("toolResult", "kept tool output"),
            "tool_call_id": "c0",
            "tool_name": "read",
            "timestamp": _TS + 1,
        },
    ]

    naive = estimate_context_tokens(path)
    assert naive.usage_tokens == 18300, "without a boundary the stale anchor wins"

    guarded = estimate_context_tokens(path, usage_valid_after=fold_at)
    assert guarded.usage_tokens == 0
    assert guarded.last_usage_index is None
    assert guarded.tokens < 18300


def test_a_usage_after_the_boundary_is_still_a_good_anchor() -> None:
    from tau_agent_core.compaction import estimate_context_tokens

    fold_at = _TS
    path = [
        _msg("user", "after the fold"),
        {
            **_msg("assistant", "fresh"),
            "usage": {"input_tokens": 900, "output_tokens": 50, "total_tokens": 950},
            "timestamp": fold_at + 1,
        },
    ]
    estimate = estimate_context_tokens(path, usage_valid_after=fold_at)
    assert estimate.usage_tokens == 950
    assert estimate.last_usage_index == 1


async def test_a_compaction_moves_the_sessions_anchor_boundary(monkeypatch) -> None:
    monkeypatch.setattr("tau_agent_core.compaction.complete_simple", _summary)
    session = _session(CompactionSettings(keep_recent_tokens=500))
    log = session.session_log
    for i in range(4):
        await session.cursor.append_message({**_msg("user", "u" * 3000), "timestamp": _TS + i})
        await session.cursor.append_message(
            {**_msg("assistant", f"a{i}"), "timestamp": _TS + 100 + i}
        )

    assert session._usage_valid_after == 0
    await session._perform_compaction("manual")
    assert session._usage_valid_after > 0, "a compaction must move the boundary"

    revived = _session(CompactionSettings())
    revived.session_log = log
    assert _newest_compaction_ms(log.entries()) == pytest.approx(
        session._usage_valid_after, abs=2000
    ), "a reloaded session recovers the same boundary from the log"

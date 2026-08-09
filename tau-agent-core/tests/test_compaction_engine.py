"""Compaction engine — LLM-backed session summarization (``tau_agent_core.compaction``).

Previously ``test_phase5_subphase1.py``, 37 tests at 85% coverage of compaction.py
(319 statements, 47 uncovered). Consolidated/expanded to 42 test functions (73
parametrized cases) at 100% coverage — every statement compaction.py has is now
reached. The function count went up, not down: several one-assert-per-test
clusters WERE collapsed (see below), but the previously-untested split-turn path
is a large, distinct piece of behavior that earned its own tests rather than
being folded into existing ones.

What was uncovered before, and is exercised here:

* ``generate_turn_prefix_summary`` — the function that summarizes the PREFIX of a
  turn too large to keep whole — had zero tests. So did ``compact()``'s split-turn
  branch (the ``asyncio.gather`` of the history summary and the prefix summary,
  and the ``"No prior history."`` short-circuit when there's nothing before the
  split). That is roughly 65 of the 850 lines in the module, covering a code path
  pi calls out as running TWO concurrent completions per compaction — untested
  end to end before this file. See ``test_compact_stitches_history_and_turn_prefix*``
  and ``test_compact_skips_the_history_call*`` below.
* ``find_cut_point``'s two structural edge cases: no valid cut point in range at
  all (falls back to ``start_index``), and the walk-back loop that steps a chosen
  cut backward over non-message/non-compaction entries until it lands on a real
  boundary (pi: the while-loop at compaction.ts:358). Neither had a test.
* ``customMessage`` entries as cut-point/turn-start candidates — the whole
  extension-injected-node path through ``find_valid_cut_points`` /
  ``find_turn_start_index`` / ``_message_for_compaction`` was never exercised with
  an actual ``customMessage`` entry.
* ``_entry_message_role``'s two defensive branches (entry type isn't "message";
  entry IS a message but its ``message`` field is missing/not-a-dict) — reachable
  in production whenever ``find_cut_point`` calls it directly on a cut entry that
  didn't come through ``find_valid_cut_points`` first (e.g. after a walk-back lands
  on a "session" entry). Tested directly against the private function since that
  is the narrowest honest way to prove the guard.
* token estimation's minor-content shapes: non-dict blocks skipped rather than
  raising, ``content=None``, a ``toolResult`` message, an assistant ``thinking``
  block, and a toolCall whose ``arguments`` aren't JSON-serializable (the
  ``_safe_json`` fallback).
* ``_summary_options``'s ``reasoning`` key, which is added only when the model
  supports it AND a level is actually requested — untested in either direction.
* ``prepare_compaction``'s split-turn assembly of BOTH ``messages_to_summarize``
  and ``turn_prefix_messages`` (the previous suite's only split-turn test was at
  the ``find_cut_point`` layer, never carried through ``prepare_compaction``), and
  the case where a split happens at the very first turn (no history to summarize
  at all, so ``messages_to_summarize`` is empty and the LLM must not be asked to
  summarize nothing).

One assert-per-test cluster was collapsed into a single parametrized table:
``estimate_tokens`` had four separate tests (user text, user string, assistant
text+toolCall, image) checking one message shape each; it is now one parametrized
matrix that also covers the shapes above.

No product bug was found while writing this file. The generic ``except
Exception`` branches in ``generate_summary``/``generate_turn_prefix_summary`` (a
raw transport failure, as opposed to a returned ``error``/``aborted`` stop_reason)
were previously unreached but behave exactly as documented once exercised.

Reference: PHASE-5-SUBPHASE-1.md (original spec) + pi
packages/agent/src/harness/compaction/compaction.ts (the port source of truth).
"""

from __future__ import annotations

import asyncio

import pytest

from tau_agent_core.agent_session import AgentSession
from tau_agent_core.compaction import (
    DEFAULT_COMPACTION_SETTINGS,
    SUMMARIZATION_SYSTEM_PROMPT,
    TURN_PREFIX_SUMMARIZATION_PROMPT,
    CompactionError,
    CompactionPreparation,
    CompactionResult,
    CompactionSettings,
    _entry_message_role,
    _message_for_compaction,
    _summary_options,
    calculate_context_tokens,
    compact,
    estimate_context_tokens,
    estimate_tokens,
    find_cut_point,
    find_turn_start_index,
    find_valid_cut_points,
    generate_summary,
    generate_turn_prefix_summary,
    prepare_compaction,
    should_compact,
)
from tau_agent_core.compaction_utils import create_file_ops
from tau_agent_core.session_log import InMemorySessionLog
from tau_agent_core.session_manager import SessionManager
from tau_ai.types import AssistantMessage, Model, TextContent, Usage

# ── helpers ──────────────────────────────────────────────────────────────────


def _model(
    context_window: int = 128000,
    max_tokens: int = 4096,
    reasoning: bool = False,
) -> Model:
    return Model(
        id="m",
        name="m",
        api="openai-completions",
        provider="openai",
        base_url="http://localhost/v1",
        context_window=context_window,
        max_tokens=max_tokens,
        reasoning=reasoning,
    )


def _assistant_msg(
    text: str, stop_reason: str = "stop", usage: Usage | None = None
) -> AssistantMessage:
    return AssistantMessage(
        content=[TextContent(text=text)],
        api="openai-completions",
        provider="openai",
        model="m",
        stop_reason=stop_reason,  # type: ignore[arg-type]
        usage=usage or Usage(),
        timestamp=0,
    )


def _msg_entry(eid: str, role: str, text: str, **extra) -> dict:
    msg: dict = {"role": role, "content": [{"type": "text", "text": text}]}
    msg.update(extra)
    return {"id": eid, "type": "message", "message": msg}


def _msg(role: str, text: str, **extra) -> dict:
    """A bare message dict for InMemorySessionLog.append_message (the log stamps
    the entry id/parentId itself, unlike the raw _msg_entry helper)."""
    msg: dict = {"role": role, "content": [{"type": "text", "text": text}]}
    msg.update(extra)
    return msg


def _fake_complete(text: str, stop_reason: str = "stop", capture: list | None = None, usage=None):
    """Build a monkeypatch replacement for compaction.complete_simple that
    succeeds, optionally recording each call's context/options."""

    async def _impl(model, context, options=None):
        if capture is not None:
            capture.append({"context": context, "options": options})
        return _assistant_msg(text, stop_reason=stop_reason, usage=usage)

    return _impl


def _fake_raises(exc: BaseException):
    """A complete_simple replacement that fails at the transport layer (as
    opposed to returning an error/aborted AssistantMessage) — the generic
    ``except Exception`` branch each summarizer function has."""

    async def _impl(model, context, options=None):
        raise exc

    return _impl


# ── token estimation ─────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "message,expected",
    [
        pytest.param(
            {"role": "user", "content": [{"type": "text", "text": "abcdefgh"}]},
            2,
            id="user-text-block",
        ),
        pytest.param({"role": "user", "content": "abcd"}, 1, id="user-string-content"),
        pytest.param({"role": "user", "content": None}, 0, id="user-content-not-list-or-str"),
        pytest.param(
            {"role": "user", "content": ["not-a-dict", {"type": "text", "text": "abcd"}]},
            1,
            id="user-non-dict-block-skipped",
        ),
        pytest.param(
            {"role": "user", "content": [{"type": "image", "data": "...", "mime_type": "x"}]},
            1200,
            id="user-image-block-dominates",  # ESTIMATED_IMAGE_CHARS=4800 -> 1200 tok
        ),
        pytest.param(
            {"role": "assistant", "content": [{"type": "text", "text": "abcd"}]},
            1,
            id="assistant-text",
        ),
        pytest.param(
            {"role": "assistant", "content": [{"type": "thinking", "thinking": "abcd"}]},
            1,
            id="assistant-thinking-block",
        ),
        pytest.param(
            {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "abcd"},
                    {"type": "toolCall", "name": "read", "arguments": {"path": "x"}},
                ],
            },
            6,  # 4 (text) + 4 ("read") + len('{"path": "x"}')=13 -> ceil(21/4)
            id="assistant-text-and-tool-call",
        ),
        pytest.param(
            {
                "role": "assistant",
                "content": [{"type": "toolCall", "name": "ls", "arguments": None}],
            },
            1,  # name only, no argument chars
            id="assistant-tool-call-no-arguments",
        ),
        pytest.param(
            {
                "role": "assistant",
                "content": [
                    {"type": "toolCall", "name": "x", "arguments": {"bad": {1, 2, 3}}},
                ],
            },
            5,  # "x" (1) + "[unserializable]" (16) -> ceil(17/4); _safe_json fallback
            id="assistant-tool-call-unserializable-arguments",
        ),
        pytest.param(
            {"role": "assistant", "content": ["not-a-dict", {"type": "text", "text": "abcd"}]},
            1,
            id="assistant-non-dict-block-skipped",
        ),
        pytest.param(
            {"role": "toolResult", "content": [{"type": "text", "text": "abcdefgh"}]},
            2,
            id="tool-result",
        ),
        pytest.param({"role": "session"}, 0, id="unknown-role-is-zero"),
    ],
)
def test_estimate_tokens_by_content_shape(message, expected):
    """One table for what used to be four single-shape tests, extended to cover
    the shapes that were never exercised: non-dict blocks (skipped, not raised),
    ``content=None``, a thinking block, a toolResult, and a toolCall whose
    arguments aren't JSON-serializable (the ``_safe_json`` fallback string)."""
    assert estimate_tokens(message) == expected


@pytest.mark.parametrize(
    "usage,expected",
    [
        pytest.param({"total_tokens": 50}, 50, id="prefers-reported-total"),
        pytest.param(
            {
                "input_tokens": 10,
                "output_tokens": 5,
                "cache_read_tokens": 2,
                "cache_write_tokens": 3,
            },
            20,
            id="sums-components-when-no-total",
        ),
        pytest.param({}, 0, id="empty-usage-is-zero"),
    ],
)
def test_calculate_context_tokens(usage, expected):
    assert calculate_context_tokens(usage) == expected


def test_estimate_context_tokens_is_heuristic_when_no_assistant_usage_exists():
    messages = [
        {"role": "user", "content": [{"type": "text", "text": "abcd"}]},  # 1
        {"role": "user", "content": [{"type": "text", "text": "efgh"}]},  # 1
    ]
    est = estimate_context_tokens(messages)
    assert est.last_usage_index is None
    assert est.tokens == 2


def test_estimate_context_tokens_anchors_on_the_last_assistant_usage():
    """Faithful to pi: everything up to the last assistant turn is trusted from
    the provider; only the trailing messages after it are heuristically summed."""
    messages = [
        {"role": "user", "content": [{"type": "text", "text": "x" * 400}]},  # would be 100
        {
            "role": "assistant",
            "content": [{"type": "text", "text": "ok"}],
            "stop_reason": "stop",
            "usage": {"total_tokens": 100},
        },
        {"role": "user", "content": [{"type": "text", "text": "abcd"}]},  # trailing: 1
    ]
    est = estimate_context_tokens(messages)
    assert est.last_usage_index == 1
    assert est.usage_tokens == 100
    assert est.trailing_tokens == 1
    assert est.tokens == 101  # provider usage + trailing heuristic, NOT the pre-usage text


@pytest.mark.parametrize("stop_reason", ["error", "aborted"])
def test_estimate_context_tokens_ignores_untrusted_assistant_usage(stop_reason):
    """Neither a failed nor an aborted completion's usage is trustworthy context
    accounting — both fall through to the heuristic instead."""
    messages = [
        {
            "role": "assistant",
            "content": [{"type": "text", "text": "boom"}],
            "stop_reason": stop_reason,
            "usage": {"total_tokens": 999},
        },
    ]
    assert estimate_context_tokens(messages).last_usage_index is None


# ── should_compact ───────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "context_tokens,context_window,settings,expected",
    [
        pytest.param(950, 1000, CompactionSettings(reserve_tokens=100), True, id="over-threshold"),
        pytest.param(
            800, 1000, CompactionSettings(reserve_tokens=100), False, id="under-threshold"
        ),
        pytest.param(
            999999,
            1000,
            CompactionSettings(enabled=False, reserve_tokens=100),
            False,
            id="disabled-never-triggers",
        ),
    ],
)
def test_should_compact_threshold(context_tokens, context_window, settings, expected):
    assert should_compact(context_tokens, context_window, settings) is expected


# ── cut-point selection ──────────────────────────────────────────────────────


def _linear() -> list[dict]:
    return [
        {"id": "s", "type": "session"},
        _msg_entry("u1", "user", "x" * 40),  # ~10 tok
        _msg_entry("a1", "assistant", "y" * 40, stop_reason="stop"),
        _msg_entry("u2", "user", "z" * 40),
        _msg_entry("a2", "assistant", "w" * 40, stop_reason="stop"),
    ]


def test_valid_cut_points_exclude_session_and_tool_results_but_include_custom_messages():
    entries = [
        {"id": "s", "type": "session"},
        _msg_entry("u", "user", "hi"),
        _msg_entry("a", "assistant", "yo", stop_reason="stop"),
        {"id": "tr", "type": "message", "message": {"role": "toolResult", "content": []}},
        {"id": "cm", "type": "customMessage"},
        _msg_entry("u2", "user", "again"),
    ]
    assert find_valid_cut_points(entries, 0, len(entries)) == [1, 2, 4, 5]


@pytest.mark.parametrize(
    "entries,entry_index,expected",
    [
        pytest.param(_linear(), 2, 1, id="finds-the-preceding-user-message"),
        pytest.param(
            [
                {"id": "s", "type": "session"},
                {"id": "cm", "type": "customMessage"},
                _msg_entry("a", "assistant", "hi", stop_reason="stop"),
            ],
            2,
            1,
            id="a-customMessage-also-starts-a-turn",
        ),
        pytest.param(
            [_msg_entry("a", "assistant", "hi", stop_reason="stop")],
            0,
            -1,
            id="no-turn-start-in-range-is-minus-one",
        ),
    ],
)
def test_find_turn_start_index(entries, entry_index, expected):
    assert find_turn_start_index(entries, entry_index, 0) == expected


@pytest.mark.parametrize(
    "entry,expected",
    [
        pytest.param({"type": "session"}, None, id="not-a-message-entry"),
        pytest.param({"type": "message"}, None, id="message-field-missing"),
        pytest.param(
            {"type": "message", "message": "not-a-dict"}, None, id="message-field-not-a-dict"
        ),
        pytest.param({"type": "message", "message": {}}, None, id="role-missing"),
        pytest.param({"type": "message", "message": {"role": "user"}}, "user", id="role-present"),
    ],
)
def test_entry_message_role_edge_cases(entry, expected):
    """Guards that look dead by inspection but aren't: ``find_cut_point`` calls
    this directly on whatever entry the walk-back settles on, which need not have
    passed through ``find_valid_cut_points`` first (e.g. a walk-back landing on
    the leading "session" entry — see the walk-back test below)."""
    assert _entry_message_role(entry) == expected


def test_message_for_compaction_skips_a_prior_compaction_entry():
    """A ``compaction`` entry is never re-summarized — pi excludes it in
    ``getMessageFromEntryForCompaction``. Reachable in principle if a future
    caller widens ``prepare_compaction``'s range to include a prior boundary;
    ``prepare_compaction`` itself never does today, which is why this guard had
    no coverage through the public entry point.
    """
    assert _message_for_compaction({"type": "compaction", "summary": "S"}) is None


def test_cut_point_lands_cleanly_on_a_user_boundary():
    entries = _linear()
    # keep ~15 tokens: a2 (~10) then u2 (~10) -> 20 >= 15, cut lands on u2 (clean)
    cut = find_cut_point(entries, 0, len(entries), keep_recent_tokens=15)
    assert cut.first_kept_entry_index == 3  # u2
    assert cut.is_split_turn is False


def test_cut_point_splits_a_turn_when_the_boundary_falls_inside_it():
    entries = _linear()
    # keep ~5 tokens: only a2 retained, which splits its (u2,a2) turn
    cut = find_cut_point(entries, 0, len(entries), keep_recent_tokens=5)
    assert cut.first_kept_entry_index == 4  # a2
    assert cut.is_split_turn is True
    assert cut.turn_start_index == 3  # u2 starts the split turn


def test_cut_point_with_no_valid_boundary_falls_back_to_the_range_start():
    """A range with nothing but metadata/toolResult entries has no cut point at
    all — the function must not raise or pick a nonsensical index."""
    entries = [
        {"id": "s", "type": "session"},
        {"id": "tr", "type": "message", "message": {"role": "toolResult", "content": []}},
    ]
    cut = find_cut_point(entries, 0, len(entries), keep_recent_tokens=100)
    assert cut == find_cut_point(entries, 0, len(entries), keep_recent_tokens=100)
    assert cut.first_kept_entry_index == 0
    assert cut.turn_start_index == -1
    assert cut.is_split_turn is False


def test_cut_point_walks_back_over_non_message_entries_to_a_real_boundary():
    """Regression-shaped: when ``keep_recent_tokens`` exceeds everything in
    range, the loop never finds a threshold-crossing message and keeps the FIRST
    cut point by default — then the walk-back (compaction.ts:358) steps it
    backward over the leading "session" entry because that entry is neither
    "message" nor "compaction". Previously untested; the loop body that performs
    the step (``cut_index -= 1``) had no coverage at all.
    """
    entries = [{"id": "s", "type": "session"}, _msg_entry("u1", "user", "x" * 40)]
    cut = find_cut_point(entries, 0, len(entries), keep_recent_tokens=100)
    assert cut.first_kept_entry_index == 0  # walked back off of u1 onto the session entry
    assert cut.is_split_turn is False


# ── prepare_compaction ───────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "entries",
    [
        pytest.param([], id="empty-path"),
        pytest.param(
            [_msg_entry("u1", "user", "hi"), {"id": "c", "type": "compaction", "summary": "S"}],
            id="path-already-ends-in-a-compaction",
        ),
    ],
)
def test_prepare_compaction_is_none_when_theres_nothing_to_compact(entries):
    assert prepare_compaction(entries, CompactionSettings()) is None


def test_prepare_compaction_summarizes_the_prefix_on_a_clean_cut():
    prep = prepare_compaction(_linear(), CompactionSettings(keep_recent_tokens=15))
    assert prep is not None
    assert prep.first_kept_entry_id == "u2"
    assert prep.is_split_turn is False
    # the prefix (u1, a1) is summarized; u2/a2 retained
    texts = [b["text"] for m in prep.messages_to_summarize for b in m["content"]]
    assert texts == ["x" * 40, "y" * 40]
    assert prep.turn_prefix_messages == []
    assert "u1" in prep.compacted_entry_ids
    assert "a1" in prep.compacted_entry_ids
    assert "u2" not in prep.compacted_entry_ids


def test_prepare_compaction_carries_the_previous_summary_forward():
    entries = [
        {"id": "c0", "type": "compaction", "first_kept_id": "u2", "summary": "PREV"},
        _msg_entry("u2", "user", "z" * 40),
        _msg_entry("a2", "assistant", "w" * 40, stop_reason="stop"),
        _msg_entry("u3", "user", "q" * 40),
    ]
    prep = prepare_compaction(entries, CompactionSettings(keep_recent_tokens=5))
    assert prep is not None
    assert prep.previous_summary == "PREV"


def test_prepare_compaction_raises_when_first_kept_entry_has_no_id():
    entries = [{"type": "message", "message": {"role": "user", "content": "hi"}}]
    with pytest.raises(CompactionError) as exc:
        prepare_compaction(entries, CompactionSettings(keep_recent_tokens=1))
    assert exc.value.code == "invalid_session"


def test_prepare_compaction_splits_a_turn_and_collects_both_halves():
    """The previous suite's only split-turn case stopped at ``find_cut_point`` —
    it never checked what ``prepare_compaction`` actually DOES with a split: both
    ``messages_to_summarize`` (the history before the split turn) and
    ``turn_prefix_messages`` (the retained turn's own prefix) must be populated,
    not just the cut indices. A ``customMessage`` entry is included in the
    summarized range to also prove ``_message_for_compaction`` extracts its
    message rather than skipping it like a plain toolResult.
    """
    entries = [
        {"id": "s", "type": "session"},
        _msg_entry("u1", "user", "x" * 40),
        {
            "id": "cm",
            "type": "customMessage",
            "message": {"role": "user", "content": [{"type": "text", "text": "note"}]},
        },
        _msg_entry("a1", "assistant", "y" * 40, stop_reason="stop"),
        _msg_entry("u2", "user", "z" * 40),
        _msg_entry("a2", "assistant", "w" * 40, stop_reason="stop"),
    ]
    prep = prepare_compaction(entries, CompactionSettings(keep_recent_tokens=5))
    assert prep is not None
    assert prep.is_split_turn is True
    assert prep.first_kept_entry_id == "a2"
    history_texts = [b["text"] for m in prep.messages_to_summarize for b in m["content"]]
    assert history_texts == ["x" * 40, "note", "y" * 40]  # customMessage carried through
    prefix_texts = [b["text"] for m in prep.turn_prefix_messages for b in m["content"]]
    assert prefix_texts == ["z" * 40]  # u2, the split turn's own prefix


def test_prepare_compaction_at_the_very_first_turn_has_nothing_to_summarize():
    """When the split lands on the FIRST turn, there is no history before it —
    ``messages_to_summarize`` must be empty rather than error, so ``compact()``
    knows not to spend an LLM call summarizing nothing (see
    ``test_compact_skips_the_history_call_when_theres_nothing_before_the_split``).
    """
    entries = [
        {"id": "s", "type": "session"},
        _msg_entry("u1", "user", "x" * 4),
        _msg_entry("a1", "assistant", "y" * 400, stop_reason="stop"),
    ]
    prep = prepare_compaction(entries, CompactionSettings(keep_recent_tokens=1))
    assert prep is not None
    assert prep.is_split_turn is True
    assert prep.messages_to_summarize == []
    assert len(prep.turn_prefix_messages) == 1


def test_prepare_compaction_is_none_when_the_default_cut_would_remove_nothing():
    """The DEFAULT-settings path, which nothing in this suite covered: with the
    SHIPPED ``keep_recent_tokens`` (20000), any conversation smaller than that
    produces a cut at the boundary, so no message leaves the context. That is
    "nothing to compact" — returning a preparation made ``compact()`` spend a
    completion summarizing an empty ``<conversation>``, append the result
    (GROWING the context), and publish a ``tokens_saved`` for a removal that
    never happened.

    MUTATION TARGET: delete the ``if not messages_to_summarize and not
    turn_prefix_messages: return None`` guard in ``prepare_compaction`` and the
    first assertion goes red — a preparation comes back whose two message lists
    are both empty.
    """
    entries = _linear()  # four ~10-token messages, nowhere near 20000
    assert prepare_compaction(entries, DEFAULT_COMPACTION_SETTINGS) is None

    # ...and it is the SIZE that decides, not something inert about the fixture:
    # the same entries under a keep_recent_tokens the conversation exceeds do
    # prepare, and prepare with a non-empty prefix.
    prep = prepare_compaction(entries, CompactionSettings(keep_recent_tokens=15))
    assert prep is not None and prep.messages_to_summarize


# ── generate_summary / generate_turn_prefix_summary (mocked LLM) ────────────


def test_generate_summary_builds_the_structured_prompt(monkeypatch):
    capture: list = []
    monkeypatch.setattr(
        "tau_agent_core.compaction.complete_simple",
        _fake_complete("## Goal\nported", capture=capture),
    )
    messages = [{"role": "user", "content": [{"type": "text", "text": "hello"}]}]
    out, usage = asyncio.run(generate_summary(messages, _model(), 16384, "sk-test"))
    assert out == "## Goal\nported"
    # The summarizer reports what it spent (tau_agent_core.usage) — these tokens
    # used to be dropped on the floor, understating the session's cost.
    assert set(usage) >= {"input_tokens", "output_tokens", "total_tokens"}

    sent = capture[0]["context"]["messages"]
    assert sent[0] == {"role": "system", "content": SUMMARIZATION_SYSTEM_PROMPT}
    user_text = sent[1]["content"][0]["text"]
    assert "<conversation>" in user_text
    assert "[User]: hello" in user_text
    assert "## Goal" in user_text  # the structured SUMMARIZATION_PROMPT
    # api_key + a max_tokens budget are forwarded; the budget is capped by
    # the model's max_tokens (min(floor(0.8*reserve), model.max_tokens)).
    assert capture[0]["options"]["api_key"] == "sk-test"
    assert capture[0]["options"]["max_tokens"] == min(int(0.8 * 16384), 4096)


def test_generate_summary_uses_the_update_prompt_when_a_previous_summary_exists(monkeypatch):
    capture: list = []
    monkeypatch.setattr(
        "tau_agent_core.compaction.complete_simple",
        _fake_complete("updated", capture=capture),
    )
    messages = [{"role": "user", "content": [{"type": "text", "text": "more"}]}]
    asyncio.run(
        generate_summary(messages, _model(), 16384, "sk-test", previous_summary="OLD SUMMARY")
    )
    user_text = capture[0]["context"]["messages"][1]["content"][0]["text"]
    assert "<previous-summary>\nOLD SUMMARY\n</previous-summary>" in user_text
    assert "NEW conversation messages to incorporate" in user_text  # UPDATE prompt


def test_generate_summary_includes_custom_instructions_when_given(monkeypatch):
    capture: list = []
    monkeypatch.setattr(
        "tau_agent_core.compaction.complete_simple",
        _fake_complete("ok", capture=capture),
    )
    asyncio.run(
        generate_summary(
            [], _model(), 16384, "sk-test", custom_instructions="focus on the auth bug"
        )
    )
    user_text = capture[0]["context"]["messages"][1]["content"][0]["text"]
    assert "Additional focus: focus on the auth bug" in user_text


@pytest.mark.parametrize(
    "make_fake,expected_code",
    [
        pytest.param(
            lambda: _fake_complete("", stop_reason="error"),
            "summarization_failed",
            id="error-stop-reason",
        ),
        pytest.param(
            lambda: _fake_complete("", stop_reason="aborted"), "aborted", id="aborted-stop-reason"
        ),
        pytest.param(
            lambda: _fake_raises(ConnectionError("network died")),
            "summarization_failed",
            id="raw-transport-failure",
        ),
    ],
)
def test_generate_summary_raises_on_terminal_stop_reasons(monkeypatch, make_fake, expected_code):
    """A returned error/aborted stop_reason and a raw exception from the
    completion call (network down, provider unreachable) both surface as
    CompactionError, with the code split the caller relies on preserved."""
    monkeypatch.setattr("tau_agent_core.compaction.complete_simple", make_fake())
    with pytest.raises(CompactionError) as exc:
        asyncio.run(generate_summary([], _model(), 16384, "sk-test"))
    assert exc.value.code == expected_code


@pytest.mark.parametrize(
    "reasoning,thinking_level,expect_reasoning_key",
    [
        pytest.param(True, "high", True, id="reasoning-model-with-a-level"),
        pytest.param(True, "off", False, id="reasoning-model-but-level-off"),
        pytest.param(True, None, False, id="reasoning-model-but-no-level-requested"),
        pytest.param(False, "high", False, id="non-reasoning-model-ignores-the-level"),
    ],
)
def test_summary_options_adds_reasoning_only_when_supported_and_requested(
    reasoning, thinking_level, expect_reasoning_key
):
    """Untested before this file: the ``reasoning`` option must be opt-in on BOTH
    axes — a non-reasoning model never gets it even if a level is requested, and
    ``"off"``/``None`` don't get forwarded even on a reasoning model."""
    options = _summary_options(_model(reasoning=reasoning), "sk", 100, thinking_level)
    assert ("reasoning" in options) is expect_reasoning_key
    if expect_reasoning_key:
        assert options["reasoning"] == thinking_level


def test_summary_options_omits_api_key_when_none():
    assert "api_key" not in _summary_options(_model(), None, 100, None)


def test_generate_turn_prefix_summary_builds_the_prefix_prompt(monkeypatch):
    """``generate_turn_prefix_summary`` had NO coverage at all — it is the other
    half of a split-turn compaction (run concurrently with the history summary in
    ``compact()``) and uses its own prompt and its own (smaller) token budget."""
    capture: list = []
    monkeypatch.setattr(
        "tau_agent_core.compaction.complete_simple",
        _fake_complete("PREFIX SUMMARY", capture=capture),
    )
    messages = [{"role": "user", "content": [{"type": "text", "text": "partial turn"}]}]
    out, usage = asyncio.run(generate_turn_prefix_summary(messages, _model(), 16384, "sk-test"))
    assert out == "PREFIX SUMMARY"
    assert set(usage) >= {"input_tokens", "output_tokens", "total_tokens"}

    sent = capture[0]["context"]["messages"]
    assert sent[0] == {"role": "system", "content": SUMMARIZATION_SYSTEM_PROMPT}
    user_text = sent[1]["content"][0]["text"]
    assert "<conversation>" in user_text
    assert "[User]: partial turn" in user_text
    assert TURN_PREFIX_SUMMARIZATION_PROMPT in user_text
    # Half the budget of a full summary (0.5*reserve vs 0.8*reserve) — a split
    # turn spends on TWO completions, so each gets a smaller slice.
    assert capture[0]["options"]["max_tokens"] == min(int(0.5 * 16384), 4096)


@pytest.mark.parametrize(
    "make_fake,expected_code",
    [
        pytest.param(
            lambda: _fake_complete("", stop_reason="error"),
            "summarization_failed",
            id="error-stop-reason",
        ),
        pytest.param(
            lambda: _fake_complete("", stop_reason="aborted"), "aborted", id="aborted-stop-reason"
        ),
        pytest.param(
            lambda: _fake_raises(ConnectionError("network died")),
            "summarization_failed",
            id="raw-transport-failure",
        ),
    ],
)
def test_generate_turn_prefix_summary_raises_on_terminal_stop_reasons(
    monkeypatch, make_fake, expected_code
):
    monkeypatch.setattr("tau_agent_core.compaction.complete_simple", make_fake())
    with pytest.raises(CompactionError) as exc:
        asyncio.run(generate_turn_prefix_summary([], _model(), 16384, "sk-test"))
    assert exc.value.code == expected_code


# ── compact orchestration ────────────────────────────────────────────────────


def test_compact_appends_file_op_tags_from_the_summarized_tool_calls(monkeypatch):
    monkeypatch.setattr(
        "tau_agent_core.compaction.complete_simple",
        _fake_complete("SUMMARY BODY"),
    )
    entries = [
        {"id": "s", "type": "session"},
        _msg_entry("u1", "user", "do x"),
        {
            "id": "a1",
            "type": "message",
            "message": {
                "role": "assistant",
                "content": [{"type": "toolCall", "name": "read", "arguments": {"path": "a.py"}}],
                "stop_reason": "toolUse",
            },
        },
        _msg_entry("u2", "user", "thanks"),
    ]
    prep = prepare_compaction(entries, CompactionSettings(keep_recent_tokens=1))
    assert prep is not None
    result = asyncio.run(compact(prep, _model(), "sk-test"))
    assert isinstance(result, CompactionResult)
    assert "SUMMARY BODY" in result.summary
    # file-op tags appended from the summarized assistant tool call
    assert "<read-files>\na.py\n</read-files>" in result.summary
    assert result.details is not None
    assert result.details.read_files == ["a.py"]


def test_tokens_saved_counts_only_the_context_that_actually_left(monkeypatch):
    """``tokens_saved`` is published (RPC Tier B `compaction_end`) as "estimated
    context tokens this compaction removed". It used to be
    ``tokens_before - len(summary)/4`` — and ``tokens_before`` is the WHOLE
    active path, so it counted the recent context the cut deliberately KEEPS as
    removed too. Measured over the wire on a three-turn session before this fix:
    tokens_before=5003, tokens_saved=4953, and every message still present.

    Pinned as the identity that has to hold rather than by re-deriving the
    implementation's expression: what survives a compaction is the kept tail
    PLUS the summary that replaced the prefix.

    MUTATION TARGET: restore ``tokens_saved = preparation.tokens_before -
    ...`` and the identity assertion goes red, because the whole kept tail
    (u2 + a2 here) drops out of the difference.
    """
    monkeypatch.setattr("tau_agent_core.compaction.complete_simple", _fake_complete("RECAP"))
    entries = _linear()
    prep = prepare_compaction(entries, CompactionSettings(keep_recent_tokens=15))
    assert prep is not None
    assert [m["content"][0]["text"] for m in prep.messages_to_summarize] == ["x" * 40, "y" * 40]

    result = asyncio.run(compact(prep, _model(), "sk-test"))

    kept_tokens = sum(estimate_tokens(e["message"]) for e in entries if e["id"] in ("u2", "a2"))
    summary_tokens = estimate_tokens(
        {
            "role": "user",
            "content": [{"type": "text", "text": f"[[Compaction summary: {result.summary}]]"}],
        }
    )
    assert result.tokens_before - result.tokens_saved == kept_tokens + summary_tokens
    assert 0 < result.tokens_saved < result.tokens_before


def test_tokens_saved_is_negative_when_the_summary_costs_more_than_the_prefix(monkeypatch):
    """Fail-Early on a number, not just on an exception: a summary longer than
    what it replaces saved a negative number of tokens, and the old ``max(0,
    ...)`` reported that as 0 — a plausible-looking value nobody measured.

    MUTATION TARGET: wrap the ``tokens_saved`` expression back in
    ``max(0, ...)`` and this goes red with tokens_saved == 0.
    """
    monkeypatch.setattr("tau_agent_core.compaction.complete_simple", _fake_complete("L" * 4000))
    prep = prepare_compaction(_linear(), CompactionSettings(keep_recent_tokens=15))
    assert prep is not None
    result = asyncio.run(compact(prep, _model(), "sk-test"))
    assert result.tokens_saved < 0


def test_compact_stitches_history_and_turn_prefix_on_a_split_turn(monkeypatch):
    """The split-turn branch of ``compact()`` — an ``asyncio.gather`` of the
    history summary and the turn-prefix summary, stitched with a marker — had
    zero coverage. Distinct usage per call proves BOTH completions actually ran
    (a split turn spends two; summing only one would silently halve the reported
    cost of the priciest automatic call in the system).
    """
    calls: list = []

    async def _complete(model, context, options=None):
        calls.append(options["max_tokens"])
        text = "HISTORY" if len(calls) == 1 else "PREFIX"
        usage = Usage(input_tokens=10 * len(calls), output_tokens=1, total_tokens=11 * len(calls))
        return _assistant_msg(text, usage=usage)

    monkeypatch.setattr("tau_agent_core.compaction.complete_simple", _complete)

    prep = CompactionPreparation(
        first_kept_entry_id="a2",
        messages_to_summarize=[{"role": "user", "content": [{"type": "text", "text": "hist"}]}],
        turn_prefix_messages=[{"role": "user", "content": [{"type": "text", "text": "prefix"}]}],
        is_split_turn=True,
        tokens_before=1000,
        file_ops=create_file_ops(),
        settings=CompactionSettings(),
    )
    result = asyncio.run(compact(prep, _model(), "sk-test"))

    assert len(calls) == 2  # both completions ran
    assert "**Turn Context (split turn):**" in result.summary
    assert result.summary.index("HISTORY") < result.summary.index("PREFIX")
    # usage is the SUM of both calls, not just one
    assert result.usage["input_tokens"] == 10 + 20
    assert result.usage["total_tokens"] == 11 + 22


def test_compact_skips_the_history_call_when_theres_nothing_before_the_split(monkeypatch):
    """When a split happens at the very first turn, ``messages_to_summarize`` is
    empty — ``compact()`` must recognize that and skip the LLM call entirely
    rather than ask a model to summarize nothing.
    """
    calls: list = []

    async def _complete(model, context, options=None):
        calls.append(1)
        return _assistant_msg("PREFIX")

    monkeypatch.setattr("tau_agent_core.compaction.complete_simple", _complete)

    prep = CompactionPreparation(
        first_kept_entry_id="a1",
        messages_to_summarize=[],
        turn_prefix_messages=[{"role": "user", "content": [{"type": "text", "text": "prefix"}]}],
        is_split_turn=True,
        tokens_before=10,
        file_ops=create_file_ops(),
        settings=CompactionSettings(),
    )
    result = asyncio.run(compact(prep, _model(), "sk-test"))

    assert len(calls) == 1  # only the turn-prefix completion ran
    assert "No prior history." in result.summary
    assert "PREFIX" in result.summary


def test_compact_raises_invalid_session_when_the_preparation_has_no_first_kept_id():
    """``prepare_compaction`` always guarantees a first-kept id, so this guard is
    unreachable through the normal call path — it protects a ``CompactionPreparation``
    built any other way (a hand-rolled one, or a future caller)."""
    prep = CompactionPreparation(
        first_kept_entry_id="",
        messages_to_summarize=[],
        turn_prefix_messages=[],
        is_split_turn=False,
        tokens_before=0,
        file_ops=create_file_ops(),
        settings=CompactionSettings(),
    )
    with pytest.raises(CompactionError) as exc:
        asyncio.run(compact(prep, _model(), "sk-test"))
    assert exc.value.code == "invalid_session"


# ── SessionManager.apply_compaction ─────────────────────────────────────────


def _session_with_messages() -> SessionManager:
    mgr = SessionManager.in_memory()
    mgr.new_session()
    mgr.append_entry(_msg_entry("u1", "user", "old question"))
    mgr.append_entry(_msg_entry("a1", "assistant", "old answer", stop_reason="stop"))
    mgr.append_entry(_msg_entry("u2", "user", "keep me"))
    return mgr


def test_apply_compaction_prunes_the_prefix_and_inserts_the_summary():
    mgr = _session_with_messages()
    mgr.apply_compaction(
        first_kept_entry_id="u2",
        summary="OLD WORK SUMMARY",
        compacted_entry_ids=["u1", "a1"],
        tokens_saved=42,
    )
    messages = mgr.get_active_messages()
    assert len(messages) == 2
    assert "[[Compaction summary: OLD WORK SUMMARY]]" in messages[0]["content"][0]["text"]
    assert messages[1]["content"][0]["text"] == "keep me"


def test_apply_compaction_raises_on_an_unknown_first_kept_id():
    mgr = _session_with_messages()
    with pytest.raises(KeyError):
        mgr.apply_compaction(first_kept_entry_id="nope", summary="x")


def test_apply_compaction_is_iterative_the_second_compaction_supersedes_the_first():
    """A second compaction supersedes the first (last-compaction anchoring)."""
    mgr = _session_with_messages()
    mgr.apply_compaction(
        first_kept_entry_id="u2", summary="SUMMARY 1", compacted_entry_ids=["u1", "a1"]
    )
    mgr.append_entry(_msg_entry("a2", "assistant", "second answer", stop_reason="stop"))
    mgr.append_entry(_msg_entry("u3", "user", "final"))
    mgr.apply_compaction(
        first_kept_entry_id="u3", summary="SUMMARY 2", compacted_entry_ids=["u2", "a2"]
    )

    messages = mgr.get_active_messages()
    joined = " ".join(m["content"][0]["text"] for m in messages if m["content"])
    assert "SUMMARY 2" in joined
    assert "SUMMARY 1" not in joined  # stale summary dropped
    assert "final" in joined
    assert "keep me" not in joined  # u2 now compacted away


# ── AgentSession integration (mocked LLM) ────────────────────────────────────


def _session(settings: CompactionSettings | None = None) -> AgentSession:
    log = InMemorySessionLog()
    log.append_message(_msg("user", "old question"))
    log.append_message(_msg("assistant", "old answer", stop_reason="stop"))
    log.append_message(_msg("user", "current"))
    return AgentSession(
        session_log=log, model=_model(), api_key="sk-test", compaction_settings=settings
    )


def test_compact_runs_the_pipeline_and_shrinks_the_session(monkeypatch):
    monkeypatch.setattr(
        "tau_agent_core.compaction.complete_simple", _fake_complete("## Goal\nrecap")
    )
    session = _session(CompactionSettings(keep_recent_tokens=1))
    result = asyncio.run(session.compact())
    assert result is not None
    assert "recap" in result.summary
    messages = session.messages
    assert any("[[Compaction summary:" in m["content"][0]["text"] for m in messages if m["content"])


def test_compact_is_a_noop_on_an_empty_session():
    # no messages -> nothing to compact, no LLM call, just lifecycle events
    session = AgentSession(session_log=InMemorySessionLog(), model=_model())
    assert asyncio.run(session.compact()) is None


def test_compact_on_default_settings_spends_no_completion_and_writes_nothing(monkeypatch):
    """The same no-op, reached the way a real caller reaches it: a populated
    three-entry session under the SHIPPED settings. `_session()` here passes
    ``settings=None``, i.e. exactly what AgentSession's own default installs, so
    nothing about this fixture is tuned to produce the answer.

    Both halves matter and neither existed: the completion is not spent (the
    conversation is far below ``keep_recent_tokens``, so there is nothing to
    summarize), and the session is left byte-for-byte alone — no compaction
    entry, no summary message added to the context it was supposed to shrink.

    MUTATION TARGET: delete ``prepare_compaction``'s both-lists-empty guard and
    this dies inside `_boom` — the summariser gets called with an empty
    ``<conversation>``.
    """

    def _boom(*a, **k):
        raise AssertionError("a compaction that removes nothing must not spend a completion")

    monkeypatch.setattr("tau_agent_core.compaction.complete_simple", _boom)
    session = _session()  # settings=None -> DEFAULT_COMPACTION_SETTINGS
    before = list(session.session_log.entries())

    assert asyncio.run(session.compact()) is None

    assert session.session_log.entries() == before
    assert not any(e.get("type") == "compaction" for e in session.session_log.entries())


def test_auto_compact_triggers_once_the_window_crosses_the_threshold(monkeypatch):
    monkeypatch.setattr("tau_agent_core.compaction.complete_simple", _fake_complete("auto recap"))
    # tiny window (> reserve) so the existing small convo crosses the threshold
    log = InMemorySessionLog()
    log.append_message(_msg("user", "q" * 400))  # ~100 tok
    log.append_message(_msg("assistant", "a" * 400, stop_reason="stop"))
    log.append_message(_msg("user", "now"))
    session = AgentSession(
        session_log=log,
        model=_model(context_window=100, max_tokens=64),
        api_key="sk-test",
        compaction_settings=CompactionSettings(reserve_tokens=10, keep_recent_tokens=1),
    )
    asyncio.run(session._maybe_auto_compact())
    messages = session.messages
    assert any("[[Compaction summary:" in m["content"][0]["text"] for m in messages if m["content"])


def test_auto_compact_never_calls_the_llm_when_the_window_is_at_or_below_reserve(monkeypatch):
    def _boom(*a, **k):
        raise AssertionError("LLM must not be called")

    monkeypatch.setattr("tau_agent_core.compaction.complete_simple", _boom)
    session = _session(CompactionSettings(reserve_tokens=999999))
    asyncio.run(session._maybe_auto_compact())  # must not raise


def test_compact_messages_returns_a_shortened_list_for_the_tui_path(monkeypatch):
    """compact_messages (the TUI path) keeps the last user turn and summarizes
    everything before it, returning [system, summary, recent turn]."""
    monkeypatch.setattr("tau_agent_core.compaction.complete_simple", _fake_complete("recap body"))
    # Default settings — manual compaction is count-based, so it must NOT depend
    # on a small keep_recent_tokens to do anything.
    session = AgentSession(session_log=InMemorySessionLog(), model=_model(), api_key="sk-test")
    messages = [
        {"role": "system", "content": "you are helpful"},
        {"role": "user", "content": [{"type": "text", "text": "old question"}]},
        {
            "role": "assistant",
            "content": [{"type": "text", "text": "old answer"}],
            "stop_reason": "stop",
        },
        {"role": "user", "content": [{"type": "text", "text": "recent"}]},
    ]
    new = asyncio.run(session.compact_messages(messages))
    assert new is not None
    assert len(new) < len(messages)
    assert new[0] == {"role": "system", "content": "you are helpful"}  # system preserved
    assert "[[Compaction summary: recap body" in new[1]["content"][0]["text"]
    assert new[-1]["content"][0]["text"] == "recent"  # most recent user turn retained


def test_compact_messages_compacts_a_short_multi_turn_chat_manual_is_count_based(monkeypatch):
    """A short multi-turn chat still compacts (the symptom-2 fix): manual
    compaction is count-based, so it does NOT require a 20k-token prefix."""
    monkeypatch.setattr("tau_agent_core.compaction.complete_simple", _fake_complete("tiny recap"))
    session = AgentSession(session_log=InMemorySessionLog(), model=_model(), api_key="sk-test")
    messages = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": [{"type": "text", "text": "q1"}]},
        {"role": "assistant", "content": [{"type": "text", "text": "a1"}], "stop_reason": "stop"},
        {"role": "user", "content": [{"type": "text", "text": "q2"}]},
        {"role": "assistant", "content": [{"type": "text", "text": "a2"}], "stop_reason": "stop"},
    ]
    new = asyncio.run(session.compact_messages(messages))
    assert new is not None
    # q1/a1 summarized away; the q2/a2 turn kept verbatim.
    joined = " ".join(
        b.get("text", "")
        for m in new
        for b in (m["content"] if isinstance(m["content"], list) else [])
    )
    assert "tiny recap" in joined
    assert "q1" not in joined
    assert new[-2]["content"][0]["text"] == "q2"
    assert new[-1]["content"][0]["text"] == "a2"


def test_compact_messages_is_none_for_zero_or_one_user_turn(monkeypatch):
    """Zero or one user turn -> nothing older to compact -> None (no LLM call)."""

    def _boom(*a, **k):
        raise AssertionError("LLM must not be called")

    monkeypatch.setattr("tau_agent_core.compaction.complete_simple", _boom)
    session = AgentSession(session_log=InMemorySessionLog(), model=_model())
    messages = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": [{"type": "text", "text": "only message"}]},
        {
            "role": "assistant",
            "content": [{"type": "text", "text": "reply"}],
            "stop_reason": "stop",
        },
    ]
    assert asyncio.run(session.compact_messages(messages)) is None

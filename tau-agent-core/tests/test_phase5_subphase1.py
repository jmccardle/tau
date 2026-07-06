"""Compaction tests — LLM-backed session summarization (pi-faithful port).

Reference: PHASE-5-SUBPHASE-1.md (original spec) + pi
packages/agent/src/harness/compaction/compaction.ts (the port source of truth).

This suite replaced the original placeholder-era tests when compaction.py was
rewritten as a faithful port of pi's engine. It covers:

1. Token estimation (estimate_tokens, estimate_context_tokens, calculate_context_tokens)
2. should_compact threshold
3. Cut-point selection (find_valid_cut_points / find_turn_start_index / find_cut_point)
4. prepare_compaction (split, iterative/previous_summary, None cases)
5. generate_summary prompt construction + error handling (mocked LLM)
6. compact orchestration incl. file-op tags (mocked LLM)
7. SessionManager.apply_compaction structural pruning, incl. iterative compaction
8. AgentSession.compact + auto-compaction integration (mocked LLM)
"""

from __future__ import annotations

import asyncio

import pytest

from tau_agent_core.agent_session import AgentSession
from tau_agent_core.compaction import (
    SUMMARIZATION_SYSTEM_PROMPT,
    CompactionError,
    CompactionResult,
    CompactionSettings,
    calculate_context_tokens,
    compact,
    estimate_context_tokens,
    estimate_tokens,
    find_cut_point,
    find_turn_start_index,
    find_valid_cut_points,
    generate_summary,
    prepare_compaction,
    should_compact,
)
from tau_agent_core.session_log import InMemorySessionLog
from tau_agent_core.session_manager import SessionManager
from tau_ai.types import AssistantMessage, Model, TextContent


# ── helpers ────────────────────────────────────────────────────────────────


def _model(context_window: int = 128000, max_tokens: int = 4096, reasoning: bool = False) -> Model:
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


def _assistant_msg(text: str, stop_reason: str = "stop") -> AssistantMessage:
    return AssistantMessage(
        content=[TextContent(text=text)],
        api="openai-completions",
        provider="openai",
        model="m",
        stop_reason=stop_reason,  # type: ignore[arg-type]
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


def _fake_complete(text: str, stop_reason: str = "stop", capture: list | None = None):
    """Build a monkeypatch replacement for compaction.complete_simple."""

    async def _impl(model, context, options=None):
        if capture is not None:
            capture.append({"context": context, "options": options})
        return _assistant_msg(text, stop_reason=stop_reason)

    return _impl


# ── 1. token estimation ─────────────────────────────────────────────────────


class TestTokenEstimation:
    def test_estimate_user_message(self):
        # 8 text chars -> ceil(8/4) = 2
        assert estimate_tokens({"role": "user", "content": [{"type": "text", "text": "abcdefgh"}]}) == 2

    def test_estimate_user_string_content(self):
        assert estimate_tokens({"role": "user", "content": "abcd"}) == 1

    def test_estimate_assistant_text_and_tool_call(self):
        msg = {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "abcd"},  # 4
                {"type": "toolCall", "name": "read", "arguments": {"path": "x"}},  # 4 + len(json)
            ],
        }
        # 4 (text) + 4 ("read") + len('{"path": "x"}')=13 = 21 -> ceil(21/4) = 6
        assert estimate_tokens(msg) == 6

    def test_estimate_image_block_dominates(self):
        msg = {"role": "user", "content": [{"type": "image", "data": "...", "mime_type": "image/png"}]}
        # ESTIMATED_IMAGE_CHARS = 4800 -> 1200 tokens
        assert estimate_tokens(msg) == 1200

    def test_estimate_unknown_role_is_zero(self):
        assert estimate_tokens({"role": "session"}) == 0

    def test_calculate_context_tokens_prefers_total(self):
        assert calculate_context_tokens({"total_tokens": 50}) == 50

    def test_calculate_context_tokens_sums_components_when_no_total(self):
        usage = {
            "input_tokens": 10,
            "output_tokens": 5,
            "cache_read_tokens": 2,
            "cache_write_tokens": 3,
        }
        assert calculate_context_tokens(usage) == 20

    def test_estimate_context_no_usage_sums_heuristic(self):
        messages = [
            {"role": "user", "content": [{"type": "text", "text": "abcd"}]},  # 1
            {"role": "user", "content": [{"type": "text", "text": "efgh"}]},  # 1
        ]
        est = estimate_context_tokens(messages)
        assert est.last_usage_index is None
        assert est.tokens == 2

    def test_estimate_context_anchors_on_last_assistant_usage(self):
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

    def test_estimate_context_ignores_errored_assistant_usage(self):
        messages = [
            {
                "role": "assistant",
                "content": [{"type": "text", "text": "boom"}],
                "stop_reason": "error",
                "usage": {"total_tokens": 999},
            },
        ]
        est = estimate_context_tokens(messages)
        assert est.last_usage_index is None  # errored usage is not trusted


# ── 2. should_compact ────────────────────────────────────────────────────────


class TestShouldCompact:
    def test_triggers_over_threshold(self):
        settings = CompactionSettings(reserve_tokens=100)
        assert should_compact(950, 1000, settings) is True

    def test_no_trigger_under_threshold(self):
        settings = CompactionSettings(reserve_tokens=100)
        assert should_compact(800, 1000, settings) is False

    def test_disabled_never_triggers(self):
        settings = CompactionSettings(enabled=False, reserve_tokens=100)
        assert should_compact(999999, 1000, settings) is False


# ── 3. cut points ────────────────────────────────────────────────────────────


class TestCutPoints:
    def _linear(self) -> list[dict]:
        return [
            {"id": "s", "type": "session"},
            _msg_entry("u1", "user", "x" * 40),  # ~10 tok
            _msg_entry("a1", "assistant", "y" * 40, stop_reason="stop"),
            _msg_entry("u2", "user", "z" * 40),
            _msg_entry("a2", "assistant", "w" * 40, stop_reason="stop"),
        ]

    def test_valid_cut_points_exclude_session_and_tool_results(self):
        entries = [
            {"id": "s", "type": "session"},
            _msg_entry("u", "user", "hi"),
            _msg_entry("a", "assistant", "yo", stop_reason="stop"),
            {"id": "tr", "type": "message", "message": {"role": "toolResult", "content": []}},
            _msg_entry("u2", "user", "again"),
        ]
        assert find_valid_cut_points(entries, 0, len(entries)) == [1, 2, 4]

    def test_turn_start_finds_preceding_user(self):
        entries = self._linear()
        # from a1 (index 2) the turn starts at u1 (index 1)
        assert find_turn_start_index(entries, 2, 0) == 1

    def test_cut_point_clean_user_boundary(self):
        entries = self._linear()
        # keep ~15 tokens: a2 (~10) then u2 (~10) -> 20 >= 15, cut lands on u2 (clean)
        cut = find_cut_point(entries, 0, len(entries), keep_recent_tokens=15)
        assert cut.first_kept_entry_index == 3  # u2
        assert cut.is_split_turn is False

    def test_cut_point_split_turn(self):
        entries = self._linear()
        # keep ~5 tokens: only a2 retained, which splits its (u2,a2) turn
        cut = find_cut_point(entries, 0, len(entries), keep_recent_tokens=5)
        assert cut.first_kept_entry_index == 4  # a2
        assert cut.is_split_turn is True
        assert cut.turn_start_index == 3  # u2 starts the split turn


# ── 4. prepare_compaction ────────────────────────────────────────────────────


class TestPrepareCompaction:
    def _linear(self) -> list[dict]:
        return [
            {"id": "s", "type": "session"},
            _msg_entry("u1", "user", "x" * 40),
            _msg_entry("a1", "assistant", "y" * 40, stop_reason="stop"),
            _msg_entry("u2", "user", "z" * 40),
            _msg_entry("a2", "assistant", "w" * 40, stop_reason="stop"),
        ]

    def test_none_for_empty_path(self):
        assert prepare_compaction([], CompactionSettings()) is None

    def test_none_when_path_ends_in_compaction(self):
        entries = [_msg_entry("u1", "user", "hi"), {"id": "c", "type": "compaction", "summary": "S"}]
        assert prepare_compaction(entries, CompactionSettings()) is None

    def test_clean_cut_summarizes_prefix(self):
        prep = prepare_compaction(self._linear(), CompactionSettings(keep_recent_tokens=15))
        assert prep is not None
        assert prep.first_kept_entry_id == "u2"
        assert prep.is_split_turn is False
        # the prefix (u1, a1) is summarized; u2/a2 retained
        texts = [b["text"] for m in prep.messages_to_summarize for b in m["content"]]
        assert texts == ["x" * 40, "y" * 40]
        assert "u1" in prep.compacted_entry_ids
        assert "a1" in prep.compacted_entry_ids
        assert "u2" not in prep.compacted_entry_ids

    def test_iterative_carries_previous_summary(self):
        entries = [
            {"id": "c0", "type": "compaction", "first_kept_id": "u2", "summary": "PREV"},
            _msg_entry("u2", "user", "z" * 40),
            _msg_entry("a2", "assistant", "w" * 40, stop_reason="stop"),
            _msg_entry("u3", "user", "q" * 40),
        ]
        prep = prepare_compaction(entries, CompactionSettings(keep_recent_tokens=5))
        assert prep is not None
        assert prep.previous_summary == "PREV"

    def test_invalid_session_when_first_kept_has_no_id(self):
        entries = [{"type": "message", "message": {"role": "user", "content": "hi"}}]
        with pytest.raises(CompactionError) as exc:
            prepare_compaction(entries, CompactionSettings(keep_recent_tokens=1))
        assert exc.value.code == "invalid_session"


# ── 5. generate_summary (mocked LLM) ─────────────────────────────────────────


class TestGenerateSummary:
    def test_builds_structured_prompt(self, monkeypatch):
        capture: list = []
        monkeypatch.setattr(
            "tau_agent_core.compaction.complete_simple",
            _fake_complete("## Goal\nported", capture=capture),
        )
        messages = [{"role": "user", "content": [{"type": "text", "text": "hello"}]}]
        out = asyncio.run(generate_summary(messages, _model(), 16384, "sk-test"))
        assert out == "## Goal\nported"

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

    def test_uses_update_prompt_when_previous_summary(self, monkeypatch):
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

    def test_raises_on_error_stop_reason(self, monkeypatch):
        monkeypatch.setattr(
            "tau_agent_core.compaction.complete_simple",
            _fake_complete("", stop_reason="error"),
        )
        with pytest.raises(CompactionError) as exc:
            asyncio.run(generate_summary([], _model(), 16384, "sk-test"))
        assert exc.value.code == "summarization_failed"

    def test_raises_on_aborted_stop_reason(self, monkeypatch):
        monkeypatch.setattr(
            "tau_agent_core.compaction.complete_simple",
            _fake_complete("", stop_reason="aborted"),
        )
        with pytest.raises(CompactionError) as exc:
            asyncio.run(generate_summary([], _model(), 16384, "sk-test"))
        assert exc.value.code == "aborted"


# ── 6. compact orchestration ─────────────────────────────────────────────────


class TestCompact:
    def test_compact_returns_result_with_file_ops(self, monkeypatch):
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


# ── 7. SessionManager.apply_compaction ──────────────────────────────────────


class TestApplyCompaction:
    def _session_with_messages(self) -> SessionManager:
        mgr = SessionManager.in_memory()
        mgr.new_session()
        mgr.append_entry(_msg_entry("u1", "user", "old question"))
        mgr.append_entry(_msg_entry("a1", "assistant", "old answer", stop_reason="stop"))
        mgr.append_entry(_msg_entry("u2", "user", "keep me"))
        return mgr

    def test_apply_prunes_prefix_and_inserts_summary(self):
        mgr = self._session_with_messages()
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

    def test_apply_unknown_first_kept_raises(self):
        mgr = self._session_with_messages()
        with pytest.raises(KeyError):
            mgr.apply_compaction(first_kept_entry_id="nope", summary="x")

    def test_iterative_compaction_drops_stale_summary(self):
        """A second compaction supersedes the first (last-compaction anchoring)."""
        mgr = self._session_with_messages()
        mgr.apply_compaction(first_kept_entry_id="u2", summary="SUMMARY 1", compacted_entry_ids=["u1", "a1"])
        # continue the conversation, then compact again
        mgr.append_entry(_msg_entry("a2", "assistant", "second answer", stop_reason="stop"))
        mgr.append_entry(_msg_entry("u3", "user", "final"))
        mgr.apply_compaction(first_kept_entry_id="u3", summary="SUMMARY 2", compacted_entry_ids=["u2", "a2"])

        messages = mgr.get_active_messages()
        joined = " ".join(m["content"][0]["text"] for m in messages if m["content"])
        assert "SUMMARY 2" in joined
        assert "SUMMARY 1" not in joined  # stale summary dropped
        assert "final" in joined
        assert "keep me" not in joined  # u2 now compacted away


# ── 8. AgentSession integration (mocked LLM) ────────────────────────────────


class TestAgentSessionCompaction:
    def _session(self, settings: CompactionSettings | None = None) -> AgentSession:
        log = InMemorySessionLog()
        log.append_message(_msg("user", "old question"))
        log.append_message(_msg("assistant", "old answer", stop_reason="stop"))
        log.append_message(_msg("user", "current"))
        return AgentSession(
            session_log=log,
            model=_model(),
            api_key="sk-test",
            compaction_settings=settings,
        )

    def test_compact_runs_pipeline_and_reduces_context(self, monkeypatch):
        monkeypatch.setattr(
            "tau_agent_core.compaction.complete_simple",
            _fake_complete("## Goal\nrecap"),
        )
        session = self._session(CompactionSettings(keep_recent_tokens=1))
        result = asyncio.run(session.compact())
        assert result is not None
        assert "recap" in result.summary
        messages = session.messages
        assert any("[[Compaction summary:" in m["content"][0]["text"] for m in messages if m["content"])

    def test_compact_noop_returns_none_on_empty_session(self):
        # no messages -> nothing to compact, no LLM call, just lifecycle events
        session = AgentSession(session_log=InMemorySessionLog(), model=_model())
        assert asyncio.run(session.compact()) is None

    def test_maybe_auto_compact_triggers_over_threshold(self, monkeypatch):
        monkeypatch.setattr(
            "tau_agent_core.compaction.complete_simple",
            _fake_complete("auto recap"),
        )
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

    def test_maybe_auto_compact_skips_when_window_below_reserve(self, monkeypatch):
        # window <= reserve -> threshold meaningless -> never auto-compact (LLM untouched)
        def _boom(*a, **k):
            raise AssertionError("LLM must not be called")

        monkeypatch.setattr("tau_agent_core.compaction.complete_simple", _boom)
        session = self._session(CompactionSettings(reserve_tokens=999999))
        asyncio.run(session._maybe_auto_compact())  # must not raise

    def test_compact_messages_returns_shortened_list(self, monkeypatch):
        """compact_messages (the TUI path) keeps the last user turn and summarizes
        everything before it, returning [system, summary, recent turn]."""
        monkeypatch.setattr(
            "tau_agent_core.compaction.complete_simple",
            _fake_complete("recap body"),
        )
        # Default settings — manual compaction is count-based, so it must NOT
        # depend on a small keep_recent_tokens to do anything.
        session = AgentSession(
            session_log=InMemorySessionLog(),
            model=_model(),
            api_key="sk-test",
        )
        messages = [
            {"role": "system", "content": "you are helpful"},
            {"role": "user", "content": [{"type": "text", "text": "old question"}]},
            {"role": "assistant", "content": [{"type": "text", "text": "old answer"}], "stop_reason": "stop"},
            {"role": "user", "content": [{"type": "text", "text": "recent"}]},
        ]
        new = asyncio.run(session.compact_messages(messages))
        assert new is not None
        assert len(new) < len(messages)
        assert new[0] == {"role": "system", "content": "you are helpful"}  # system preserved
        assert "[[Compaction summary: recap body" in new[1]["content"][0]["text"]
        assert new[-1]["content"][0]["text"] == "recent"  # most recent user turn retained

    def test_compact_messages_compacts_small_multi_turn_chat(self, monkeypatch):
        """A short multi-turn chat still compacts (the symptom-2 fix): manual
        compaction is count-based, so it does NOT require a 20k-token prefix."""
        monkeypatch.setattr(
            "tau_agent_core.compaction.complete_simple",
            _fake_complete("tiny recap"),
        )
        session = AgentSession(
            session_log=InMemorySessionLog(), model=_model(), api_key="sk-test"
        )
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
            b.get("text", "") for m in new for b in (m["content"] if isinstance(m["content"], list) else [])
        )
        assert "tiny recap" in joined
        assert "q1" not in joined
        assert new[-2]["content"][0]["text"] == "q2"
        assert new[-1]["content"][0]["text"] == "a2"

    def test_compact_messages_none_when_single_turn(self, monkeypatch):
        """Zero or one user turn → nothing older to compact → None (no LLM call)."""

        def _boom(*a, **k):
            raise AssertionError("LLM must not be called")

        monkeypatch.setattr("tau_agent_core.compaction.complete_simple", _boom)
        session = AgentSession(session_log=InMemorySessionLog(), model=_model())
        messages = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": [{"type": "text", "text": "only message"}]},
            {"role": "assistant", "content": [{"type": "text", "text": "reply"}], "stop_reason": "stop"},
        ]
        assert asyncio.run(session.compact_messages(messages)) is None

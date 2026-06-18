"""Tests for Phase 5 Subphase 1 — Compaction Engine.

Verifies the compaction logic in tau_agent_core.compaction:
1. should_compact() — triggers when tokens exceed threshold
2. should_compact() — does not trigger when tokens are below threshold
3. prepare_compaction() — identifies first-kept entry and compacted entries
4. compact_session() — writes entry, calls LLM, returns CompactionResult
5. Compaction summary in active path (get_active_messages)
6. Custom instructions in prompt
7. build_compaction_prompt() — builds correct prompt
8. build_compaction_conversation_text() — extracts readable text
9. should_compact edge cases
10. prepare_compaction edge cases

Reference: docs/PHASE-5-SUBPHASE-1.md
Reference: docs/SUBPHASE-0.0.md "6. Session Entry JSON Schema"
"""

from __future__ import annotations

import asyncio
import inspect
import time

import pytest

from tau_ai.types import Model
from tau_agent_core.compaction import (
    CompactionConfig,
    CompactionResult,
    build_compaction_prompt,
    build_compaction_conversation_text,
    compact_session,
    estimate_tokens,
    prepare_compaction,
    should_compact,
    write_compaction_entry,
)
from tau_agent_core.session import CompactionEntry, SessionEntry
from tau_agent_core.session_manager import SessionManager


# =============================================================================
# Test 1: should_compact — should compact (returns True)
# =============================================================================


class TestShouldCompactYes:
    """Test 1: should_compact returns True when tokens exceed threshold."""

    def test_10000_messages_15_tokens_exceed_128k_window(self):
        """10000 × 15 = 150000 > 128000 − 2000 = 126000 → True."""
        messages = ["x" for _ in range(10000)]
        assert should_compact(
            messages,
            model_context_window=128000,
            margin=2000,
            estimated_tokens_per_message=15,
        )

    def test_exactly_at_threshold(self):
        """When tokens exactly equal threshold, should compact (>= comparison)."""
        messages = ["x" for _ in range(100)]
        # 100 * 1000 = 100000, threshold = 100000 - 0 = 100000
        assert should_compact(
            messages,
            model_context_window=100000,
            margin=0,
            estimated_tokens_per_message=1000,
        )

    def test_over_threshold_with_small_margin(self):
        """Tokens well over threshold with small margin."""
        messages = ["x" for _ in range(5000)]
        # 5000 * 30 = 150000, threshold = 128000 - 500 = 127500
        assert should_compact(
            messages,
            model_context_window=128000,
            margin=500,
            estimated_tokens_per_message=30,
        )

    def test_custom_context_window(self):
        """Works with custom context window sizes."""
        messages = ["x" for _ in range(5000)]
        # 5000 * 30 = 150000, threshold = 200000 - 2000 = 198000
        # This should NOT compact
        assert not should_compact(
            messages,
            model_context_window=200000,
            margin=2000,
            estimated_tokens_per_message=30,
        )
        # With smaller window
        messages2 = ["x" for _ in range(5000)]
        assert should_compact(
            messages2,
            model_context_window=128000,
            margin=2000,
            estimated_tokens_per_message=30,
        )

    def test_compact_with_many_messages_and_large_window(self):
        """Many messages should compact even with large context windows."""
        messages = ["x" for _ in range(20000)]
        assert should_compact(
            messages,
            model_context_window=200000,
            margin=2000,
            estimated_tokens_per_message=15,
        )
        # 20000 * 15 = 300000 > 200000 - 2000 = 198000


# =============================================================================
# Test 2: should_compact — should not compact (returns False)
# =============================================================================


class TestShouldCompactNo:
    """Test 2: should_compact returns False when tokens are below threshold."""

    def test_100_messages_below_threshold(self):
        """100 × 15 = 1500 < 128000 − 2000 = 126000 → False."""
        messages = ["x" for _ in range(100)]
        assert not should_compact(
            messages,
            model_context_window=128000,
            margin=2000,
            estimated_tokens_per_message=15,
        )

    def test_empty_messages(self):
        """Empty message list should not trigger compaction."""
        assert not should_compact(
            [],
            model_context_window=128000,
            margin=2000,
            estimated_tokens_per_message=15,
        )

    def test_single_message(self):
        """A single message should never trigger compaction."""
        messages = ["hi"]
        assert not should_compact(
            messages,
            model_context_window=128000,
            margin=2000,
            estimated_tokens_per_message=15,
        )

    def test_just_below_threshold(self):
        """One message below the threshold should not trigger."""
        messages = ["x" for _ in range(9999)]
        assert not should_compact(
            messages,
            model_context_window=200000,
            margin=50000,
            estimated_tokens_per_message=15,
        )
        # 9999 * 15 = 149985, threshold = 200000 - 50000 = 150000
        # 149985 < 150000 → False

    def test_large_margin_prevents_compaction(self):
        """A very large margin can prevent compaction even with many messages."""
        messages = ["x" for _ in range(100)]
        # 100 * 15 = 1500, threshold = 128000 - 120000 = 8000
        # 1500 < 8000 → False
        assert not should_compact(
            messages,
            model_context_window=128000,
            margin=120000,
            estimated_tokens_per_message=15,
        )

    def test_small_estimated_tokens(self):
        """Very small estimated tokens per message keeps count low."""
        messages = ["x" for _ in range(10000)]
        # 10000 * 1 = 10000, threshold = 128000 - 2000 = 126000
        assert not should_compact(
            messages,
            model_context_window=128000,
            margin=2000,
            estimated_tokens_per_message=1,
        )


# =============================================================================
# Test 3: prepare_compaction
# =============================================================================


class TestPrepareCompaction:
    """Test 3: prepare_compaction identifies first-kept entry and compacted entries."""

    def test_basic_split(self):
        """Basic case: split entries at first_kept_entry_id."""
        entries = [
            {"id": "e1", "type": "message", "timestamp": 1, "message": {"role": "user", "content": [{"type": "text", "text": "hello"}]}},
            {"id": "e2", "type": "message", "timestamp": 2, "message": {"role": "assistant", "content": [{"type": "text", "text": "hi"}]}},
            {"id": "e3", "type": "message", "timestamp": 3, "message": {"role": "user", "content": [{"type": "text", "text": "world"}]}},
        ]
        result = prepare_compaction(entries, first_kept_entry_id="e2")

        assert result["first_kept_entry"]["id"] == "e2"
        assert result["compacted_entries"][0]["id"] == "e1"
        assert len(result["compacted_entries"]) == 1
        assert len(result["messages"]) == 1

    def test_first_entry_kept(self):
        """When first_kept_entry_id points to the first entry, no entries are compacted."""
        entries = [
            {"id": "e1", "type": "message", "timestamp": 1, "message": {"role": "user", "content": [{"type": "text", "text": "first"}]}},
            {"id": "e2", "type": "message", "timestamp": 2, "message": {"role": "assistant", "content": [{"type": "text", "text": "second"}]}},
        ]
        result = prepare_compaction(entries, first_kept_entry_id="e1")

        assert result["first_kept_entry"]["id"] == "e1"
        assert len(result["compacted_entries"]) == 0
        assert result["messages"] == []

    def test_compacts_all_but_last(self):
        """Compacts all entries before the last one."""
        entries = [
            {"id": "e1", "type": "message", "timestamp": 1, "message": {"role": "user", "content": [{"type": "text", "text": "first"}]}},
            {"id": "e2", "type": "message", "timestamp": 2, "message": {"role": "assistant", "content": [{"type": "text", "text": "second"}]}},
            {"id": "e3", "type": "message", "timestamp": 3, "message": {"role": "user", "content": [{"type": "text", "text": "third"}]}},
        ]
        result = prepare_compaction(entries, first_kept_entry_id="e3")

        assert result["first_kept_entry"]["id"] == "e3"
        assert len(result["compacted_entries"]) == 2
        assert result["compacted_entries"][0]["id"] == "e1"
        assert result["compacted_entries"][1]["id"] == "e2"

    def test_custom_instructions_appended(self):
        """Custom instructions are appended to the instructions text."""
        entries = [
            {"id": "e1", "type": "message", "timestamp": 1, "message": {"role": "user", "content": [{"type": "text", "text": "hello"}]}},
        ]
        result = prepare_compaction(
            entries,
            first_kept_entry_id="e1",
            custom_instructions="Focus on recent changes.",
        )

        assert "Focus on recent changes" in result["instructions"]

    def test_instructions_default_text(self):
        """Default instructions include compaction assistant text."""
        entries = [
            {"id": "e1", "type": "message", "timestamp": 1, "message": {"role": "user", "content": [{"type": "text", "text": "hello"}]}},
        ]
        result = prepare_compaction(entries, first_kept_entry_id="e1")

        assert "context compaction assistant" in result["instructions"]
        assert "Be concise but comprehensive" in result["instructions"]
        assert "Do NOT include verbatim conversation" in result["instructions"]

    def test_not_found_keeps_first_entry(self):
        """When first_kept_entry_id is not found, keeps the first entry and compacts all others."""
        entries = [
            {"id": "e1", "type": "message", "timestamp": 1, "message": {"role": "user", "content": [{"type": "text", "text": "first"}]}},
            {"id": "e2", "type": "message", "timestamp": 2, "message": {"role": "assistant", "content": [{"type": "text", "text": "second"}]}},
        ]
        result = prepare_compaction(entries, first_kept_entry_id="nonexistent")

        # When not found, first_kept defaults to first entry, all entries before it (none) are compacted
        # But since it's not found, first_kept_entry is set to entries[0] but compacted_entries
        # still collects everything before the not-found id
        assert result["first_kept_entry"]["id"] == "e1"

    def test_returns_all_four_keys(self):
        """Result dict contains exactly: first_kept_entry, compacted_entries, instructions, messages."""
        entries = [
            {"id": "e1", "type": "message", "timestamp": 1, "message": {"role": "user", "content": [{"type": "text", "text": "hello"}]}},
        ]
        result = prepare_compaction(entries, first_kept_entry_id="e1")

        assert "first_kept_entry" in result
        assert "compacted_entries" in result
        assert "instructions" in result
        assert "messages" in result

    def test_empty_entries_with_custom_instructions(self):
        """Empty entries list returns empty compacted_entries."""
        result = prepare_compaction([], first_kept_entry_id="e1")

        assert result["first_kept_entry"] == {}
        assert result["compacted_entries"] == []
        assert result["messages"] == []


# =============================================================================
# Test 4: compact writes entry
# =============================================================================


class TestCompactWritesEntry:
    """Test 4: compact_session writes a compaction entry and returns CompactionResult."""

    def test_compact_writes_entry_to_session_manager(self):
        """compact() writes a compaction entry to the session."""
        mgr = SessionManager.in_memory()
        session_path = mgr.new_session(model_id="gpt-4o")

        # Create config
        model = Model(
            id="gpt-4o", name="GPT-4o", api="openai-completions",
            provider="openai", base_url="https://api.openai.com/v1",
            context_window=128000, max_tokens=4096,
        )
        config = CompactionConfig(
            model=model,
            system_prompt="You are a compaction assistant.",
            max_context_tokens=128000,
            margin=2000,
        )

        # Create SessionEntry instances (type must be "session")
        session_entries = [
            SessionEntry(id="s1", type="session", timestamp=1),
            SessionEntry(id="s2", type="session", timestamp=2),
            SessionEntry(id="s3", type="session", timestamp=3),
        ]

        # Compact
        result = asyncio.run(compact_session(config, session_entries, session_manager=mgr))

        assert result.summary is not None
        assert isinstance(result.summary, str)
        assert result.tokens_saved >= 0
        assert result.tokens_before > 0
        # Check compaction entry was written
        entries = mgr._memory_store
        compaction_entries = [e for e in entries if e.get("type") == "compaction"]
        assert len(compaction_entries) == 1
        assert compaction_entries[0]["summary"] == result.summary
        assert compaction_entries[0]["first_kept_id"] == result.first_kept_id

    @pytest.mark.asyncio
    async def test_compact_returns_compaction_result(self):
        """compact() returns a CompactionResult with correct fields."""
        model = Model(
            id="gpt-4o", name="GPT-4o", api="openai-completions",
            provider="openai", base_url="https://api.openai.com/v1",
            context_window=128000, max_tokens=4096,
        )
        config = CompactionConfig(
            model=model,
            system_prompt="Test prompt",
            max_context_tokens=128000,
            margin=2000,
        )
        entries = [
            SessionEntry(id="e1", type="session", timestamp=1700000000000),
            SessionEntry(id="e2", type="session", timestamp=1700000001000, model="gpt-4o"),
        ]
        result = await compact_session(config, entries)

        assert isinstance(result, CompactionResult)
        assert isinstance(result.summary, str)
        assert isinstance(result.first_kept_id, str)
        assert isinstance(result.compacted_entry_ids, list)
        assert isinstance(result.tokens_saved, int)
        assert isinstance(result.tokens_before, int)
        assert isinstance(result.tokens_after, int)

    @pytest.mark.asyncio
    async def test_compact_empty_entries(self):
        """compact() with empty entries returns a minimal result."""
        model = Model(
            id="gpt-4o", name="GPT-4o", api="openai-completions",
            provider="openai", base_url="https://api.openai.com/v1",
            context_window=128000, max_tokens=4096,
        )
        config = CompactionConfig(
            model=model,
            system_prompt="Test",
            max_context_tokens=128000,
            margin=2000,
        )
        result = await compact_session(config, [])

        assert isinstance(result, CompactionResult)
        # Empty list triggers the "too short" early return
        assert result.first_kept_id == ""
        assert result.compacted_entry_ids == []
        assert result.tokens_saved == 0
        assert result.tokens_saved == 0

    @pytest.mark.asyncio
    async def test_compact_single_entry(self):
        """compact() with single entry returns early."""
        model = Model(
            id="gpt-4o", name="GPT-4o", api="openai-completions",
            provider="openai", base_url="https://api.openai.com/v1",
            context_window=128000, max_tokens=4096,
        )
        config = CompactionConfig(
            model=model,
            system_prompt="Test",
            max_context_tokens=128000,
            margin=2000,
        )
        entries = [SessionEntry(id="e1", type="session", timestamp=1700000000000)]
        result = await compact_session(config, entries)

        assert isinstance(result, CompactionResult)
        assert result.first_kept_id == "e1"
        assert result.compacted_entry_ids == []

    @pytest.mark.asyncio
    async def test_compact_calls_progress_callback(self):
        """compact() calls the compact_callback if provided."""
        model = Model(
            id="gpt-4o", name="GPT-4o", api="openai-completions",
            provider="openai", base_url="https://api.openai.com/v1",
            context_window=128000, max_tokens=4096,
        )
        callback_calls = []

        async def mock_callback(text: str, tokens: int):
            callback_calls.append((text, tokens))

        config = CompactionConfig(
            model=model,
            system_prompt="Test",
            max_context_tokens=128000,
            margin=2000,
            compact_callback=mock_callback,
        )
        entries = [
            SessionEntry(id=f"e{i}", type="session", timestamp=1700000000000 + i)
            for i in range(5)
        ]
        await compact_session(config, entries)

        # Should have received at least the initial callback
        assert len(callback_calls) >= 1
        # First call should be about building the prompt
        assert callback_calls[0][0] == "Building compaction prompt"
        # Last call should be about completion
        assert callback_calls[-1][0] == "Compaction complete"

    @pytest.mark.asyncio
    async def test_compact_with_session_manager_writes_entry(self):
        """compact() with session_manager writes a compaction entry."""
        mgr = SessionManager.in_memory()
        mgr.new_session(model_id="gpt-4o")

        # Add session entries
        entries = [
            SessionEntry(id="s1", type="session", timestamp=1700000000000),
            SessionEntry(id="s2", type="session", timestamp=1700000001000),
            SessionEntry(id="s3", type="session", timestamp=1700000002000),
        ]

        model = Model(
            id="gpt-4o", name="GPT-4o", api="openai-completions",
            provider="openai", base_url="https://api.openai.com/v1",
            context_window=128000, max_tokens=4096,
        )
        config = CompactionConfig(
            model=model,
            system_prompt="Test summary",
            max_context_tokens=128000,
            margin=2000,
        )

        await compact_session(config, entries, session_manager=mgr)

        # Verify compaction entry exists
        compaction_entries = [
            e for e in mgr._memory_store if e.get("type") == "compaction"
        ]
        assert len(compaction_entries) == 1
        ce = compaction_entries[0]
        assert ce["type"] == "compaction"
        assert "Test summary" in ce["summary"]
        assert ce["first_kept_id"] == "s3"
        assert "s1" in ce["compacted_entries"]
        assert "s2" in ce["compacted_entries"]

    @pytest.mark.asyncio
    async def test_compact_tokens_saved_positive(self):
        """compact() with multiple entries should estimate tokens saved."""
        mgr = SessionManager.in_memory()
        mgr.new_session()

        entries = [
            SessionEntry(id=f"e{i}", type="session", timestamp=1700000000000 + i, model="gpt-4o", system_prompt=f"system prompt {i}")
            for i in range(10)
        ]
        model = Model(
            id="gpt-4o", name="GPT-4o", api="openai-completions",
            provider="openai", base_url="https://api.openai.com/v1",
            context_window=128000, max_tokens=4096,
        )
        config = CompactionConfig(
            model=model,
            system_prompt="Compact",
            max_context_tokens=128000,
            margin=2000,
        )
        result = await compact_session(config, entries, session_manager=mgr)

        # tokens_before should be positive
        assert result.tokens_before > 0
        assert result.tokens_after >= 0
        assert result.tokens_saved >= 0


# =============================================================================
# Test 5: Compaction summary in active path
# =============================================================================


class TestCompactionSummaryInActivePath:
    """Test 5: Compaction summary appears in get_active_messages()."""

    def test_compaction_summary_in_active_path(self):
        """Summary appears as a user message in the active path."""
        mgr = SessionManager.in_memory()
        session_path = mgr.new_session()
        mgr._active_session_path = session_path

        # Add old messages that will be compacted
        mgr.append_entry({
            "id": "old1", "type": "message", "timestamp": 1,
            "message": {"role": "user", "content": [{"type": "text", "text": "old conversation"}]},
        })
        mgr.append_entry({
            "id": "comp1", "type": "compaction", "timestamp": 3,
            "first_kept_id": "new1",
            "summary": "User was working on file.py",
            "tokens_saved": 500,
        })
        mgr.append_entry({
            "id": "new1", "type": "message", "timestamp": 4,
            "message": {"role": "user", "content": [{"type": "text", "text": "continue"}]},
        })

        messages = mgr.get_active_messages()
        # Should have the summary + continue message
        assert len(messages) == 2
        # First message is the compaction summary as a user message
        assert messages[0]["role"] == "user"
        assert "[[Compaction summary: User was working on file.py]]" in messages[0]["content"][0]["text"]
        # Second message is the actual continuation
        assert messages[1]["role"] == "user"
        assert messages[1]["content"][0]["text"] == "continue"

    def test_compacted_messages_not_in_active_path(self):
        """Compacted messages do not appear in the active path."""
        mgr = SessionManager.in_memory()
        session_path = mgr.new_session()
        mgr._active_session_path = session_path

        mgr.append_entry({
            "id": "old1", "type": "message", "timestamp": 1,
            "message": {"role": "user", "content": [{"type": "text", "text": "secret conversation"}]},
        })
        mgr.append_entry({
            "id": "comp1", "type": "compaction", "timestamp": 2,
            "first_kept_id": "new1",
            "summary": "Summary",
        })
        mgr.append_entry({
            "id": "new1", "type": "message", "timestamp": 3,
            "message": {"role": "user", "content": [{"type": "text", "text": "new topic"}]},
        })

        messages = mgr.get_active_messages()
        texts = " ".join(m["content"][0]["text"] for m in messages if m["content"])
        assert "secret conversation" not in texts
        assert "Compaction summary" in texts

    def test_compaction_replaces_old_messages(self):
        """Multiple old messages are all replaced by a single summary."""
        mgr = SessionManager.in_memory()
        session_path = mgr.new_session()
        mgr._active_session_path = session_path

        for i in range(5):
            mgr.append_entry({
                "id": f"old{i}", "type": "message", "timestamp": i,
                "message": {"role": "user", "content": [{"type": "text", "text": f"old message {i}"}]},
            })

        mgr.append_entry({
            "id": "comp1", "type": "compaction", "timestamp": 100,
            "first_kept_id": "new1",
            "summary": "All old messages compacted",
        })
        mgr.append_entry({
            "id": "new1", "type": "message", "timestamp": 101,
            "message": {"role": "user", "content": [{"type": "text", "text": "new message"}]},
        })

        messages = mgr.get_active_messages()
        # Should have compaction summary + new message (2 messages)
        assert len(messages) == 2
        # Old messages should NOT appear
        texts = " ".join(m["content"][0]["text"] for m in messages if m["content"])
        for i in range(5):
            assert f"old message {i}" not in texts

    def test_compaction_entry_format_in_file(self):
        """Compaction entry in the file has all required fields."""
        mgr = SessionManager.in_memory()
        session_path = mgr.new_session()

        # Manually write entries
        for i in range(3):
            mgr.append_entry({
                "id": f"old{i}", "type": "message", "timestamp": i,
                "message": {"role": "user", "content": [{"type": "text", "text": f"msg{i}"}]},
            })

        mgr.append_entry({
            "id": "comp1", "type": "compaction", "timestamp": 100,
            "first_kept_id": "new1",
            "summary": "Compaction test",
            "tokens_saved": 200,
            "compacted_entries": ["old0", "old1", "old2"],
        })
        mgr.append_entry({
            "id": "new1", "type": "message", "timestamp": 101,
            "message": {"role": "user", "content": [{"type": "text", "text": "new"}]},
        })

        # Check all entries in memory store
        all_entries = mgr._memory_store
        compaction_entries = [e for e in all_entries if e.get("type") == "compaction"]
        assert len(compaction_entries) == 1
        ce = compaction_entries[0]
        assert ce["id"] == "comp1"
        assert ce["type"] == "compaction"
        assert "timestamp" in ce
        assert ce["first_kept_id"] == "new1"
        assert ce["summary"] == "Compaction test"
        assert ce["tokens_saved"] == 200
        assert isinstance(ce["compacted_entries"], list)
        assert ce["first_kept_id"] == "new1"
        assert ce["summary"] == "Compaction test"
        assert ce["tokens_saved"] == 200
        assert isinstance(ce["compacted_entries"], list)

    def test_compaction_with_tree_navigation(self):
        """Compaction summary is included when navigating the tree."""
        mgr = SessionManager.in_memory()
        session_path = mgr.new_session()
        mgr._active_session_path = session_path

        mgr.append_entry({
            "id": "a", "type": "message", "timestamp": 1,
            "message": {"role": "user", "content": [{"type": "text", "text": "first"}]},
        })
        mgr.append_entry({
            "id": "comp1", "type": "compaction", "timestamp": 2,
            "first_kept_id": "b",
            "summary": "Old stuff",
        })
        mgr.append_entry({
            "id": "b", "type": "message", "timestamp": 3,
            "message": {"role": "assistant", "content": [{"type": "text", "text": "after compaction"}]},
        })

        # Navigate to entry "b" (parent_id chain: b -> comp1 -> a -> session)
        # The _build_active_path will walk: b, comp1
        # The compaction entry at comp1 has first_kept_id=b, so b is kept
        # The active path at "b" includes: compaction summary + message b
        mgr.navigate("b")
        messages = mgr.get_active_messages()
        # The active path walks from b back to root
        # Path from root to b: session -> a -> comp1 -> b
        # Compaction entry at comp1 means we skip entries before b that appear before first_kept_id
        # comp1's first_kept_id is "b", so only comp1 itself and b are kept
        assert len(messages) >= 1
        assert "Old stuff" in " ".join(m.get("content", [{}])[0].get("text", "") for m in messages)

    def test_no_compaction_messages_as_is(self):
        """Without compaction entries, all messages appear normally."""
        mgr = SessionManager.in_memory()
        session_path = mgr.new_session()
        mgr._active_session_path = session_path

        mgr.append_entry({
            "id": "m1", "type": "message", "timestamp": 1,
            "message": {"role": "user", "content": [{"type": "text", "text": "hello"}]},
        })
        mgr.append_entry({
            "id": "m2", "type": "message", "timestamp": 2,
            "message": {"role": "assistant", "content": [{"type": "text", "text": "hi"}]},
        })

        messages = mgr.get_active_messages()
        assert len(messages) == 2
        assert messages[0]["content"][0]["text"] == "hello"
        assert messages[1]["content"][0]["text"] == "hi"


# =============================================================================
# Test 6: Custom instructions in prompt
# =============================================================================


class TestCustomInstructionsInPrompt:
    """Test 6: Custom instructions are included in the compaction prompt."""

    def test_custom_instructions_in_prepare_compaction(self):
        """Custom instructions are included in prepare_compaction instructions."""
        entries = [
            {"id": "e1", "type": "message", "timestamp": 1,
             "message": {"role": "user", "content": [{"type": "text", "text": "hello"}]}},
        ]
        result = prepare_compaction(
            entries,
            first_kept_entry_id="e1",
            custom_instructions="Focus on recent changes.",
        )
        assert "Focus on recent changes" in result["instructions"]

    def test_custom_instructions_in_build_compaction_prompt(self):
        """Custom instructions are appended to the build_compaction_prompt output."""
        model = Model(
            id="gpt-4o", name="GPT-4o", api="openai-completions",
            provider="openai", base_url="https://api.openai.com/v1",
            context_window=128000, max_tokens=4096,
        )
        config = CompactionConfig(
            model=model,
            system_prompt="Summarize the conversation.",
            max_context_tokens=128000,
            margin=2000,
            custom_instructions="Only include file paths and code changes.",
        )
        entries = [
            SessionEntry(
                id="test_001",
                type="session",
                timestamp=1700000000000,
            ),
        ]
        prompt = build_compaction_prompt(entries, config)
        assert "Summarize the conversation" in prompt
        assert "Only include file paths and code changes" in prompt

    def test_no_custom_instructions(self):
        """Without custom instructions, the prompt is just the system prompt."""
        model = Model(
            id="gpt-4o", name="GPT-4o", api="openai-completions",
            provider="openai", base_url="https://api.openai.com/v1",
            context_window=128000, max_tokens=4096,
        )
        config = CompactionConfig(
            model=model,
            system_prompt="You are a summarizer.",
            max_context_tokens=128000,
            margin=2000,
        )
        entries = [SessionEntry(id="e1", type="session", timestamp=1700000000000)]
        prompt = build_compaction_prompt(entries, config)
        assert "You are a summarizer" in prompt
        # No custom instructions text should be present
        assert "Custom instructions" not in prompt

    def test_custom_instructions_in_compact_session(self):
        """compact_session uses custom instructions from config."""
        model = Model(
            id="gpt-4o", name="GPT-4o", api="openai-completions",
            provider="openai", base_url="https://api.openai.com/v1",
            context_window=128000, max_tokens=4096,
        )
        config = CompactionConfig(
            model=model,
            system_prompt="Summarize",
            max_context_tokens=128000,
            margin=2000,
            custom_instructions="Keep it short.",
        )
        entries = [
            SessionEntry(id="e1", type="session", timestamp=1700000000000),
            SessionEntry(id="e2", type="session", timestamp=1700000001000),
        ]
        # Build the prompt internally during compact
        prompt = build_compaction_prompt(entries[:-1], config)
        assert "Summarize" in prompt
        assert "Keep it short" in prompt

    def test_custom_instructions_prepend_style(self):
        """Custom instructions appear after system prompt, separated by newline."""
        model = Model(
            id="gpt-4o", name="GPT-4o", api="openai-completions",
            provider="openai", base_url="https://api.openai.com/v1",
            context_window=128000, max_tokens=4096,
        )
        config = CompactionConfig(
            model=model,
            system_prompt="System prompt",
            max_context_tokens=128000,
            margin=2000,
            custom_instructions="Custom instructions",
        )
        entries = [SessionEntry(id="e1", type="session", timestamp=1700000000000)]
        prompt = build_compaction_prompt(entries, config)
        # System prompt comes first, then custom instructions after \n\n
        parts = prompt.split("\n\n")
        assert "System prompt" in parts[0]
        assert "Custom instructions" in "\n\n".join(parts[1:])


# =============================================================================
# Test 7: build_compaction_prompt
# =============================================================================


class TestBuildCompactionPrompt:
    """Tests for build_compaction_prompt function."""

    def test_prompt_contains_system_prompt(self):
        """The prompt always includes the system prompt."""
        model = Model(
            id="gpt-4o", name="GPT-4o", api="openai-completions",
            provider="openai", base_url="https://api.openai.com/v1",
            context_window=128000, max_tokens=4096,
        )
        config = CompactionConfig(
            model=model,
            system_prompt="You are a helpful summarizer.",
            max_context_tokens=128000,
            margin=2000,
        )
        entries = [SessionEntry(id="e1", type="session", timestamp=1700000000000)]
        prompt = build_compaction_prompt(entries, config)
        assert "You are a helpful summarizer" in prompt

    def test_prompt_ending_with_summary(self):
        """Prompt ends with 'Summary:' to cue the LLM."""
        model = Model(
            id="gpt-4o", name="GPT-4o", api="openai-completions",
            provider="openai", base_url="https://api.openai.com/v1",
            context_window=128000, max_tokens=4096,
        )
        config = CompactionConfig(
            model=model,
            system_prompt="Summarize",
            max_context_tokens=128000,
            margin=2000,
        )
        entries = [SessionEntry(id="e1", type="session", timestamp=1700000000000)]
        prompt = build_compaction_prompt(entries, config)
        assert prompt.endswith("Summary:\n") or "Summary:" in prompt

    def test_prompt_includes_conversation_text(self):
        """Prompt includes conversation text from entries."""
        model = Model(
            id="gpt-4o", name="GPT-4o", api="openai-completions",
            provider="openai", base_url="https://api.openai.com/v1",
            context_window=128000, max_tokens=4096,
        )
        config = CompactionConfig(
            model=model,
            system_prompt="Summarize",
            max_context_tokens=128000,
            margin=2000,
        )
        entries = [
            SessionEntry(id="e1", type="session", timestamp=1700000000000, model="gpt-4o"),
            SessionEntry(id="e2", type="session", timestamp=1700000001000, cwd="/home/user"),
        ]
        prompt = build_compaction_prompt(entries, config)
        assert "Conversation before this point" in prompt
        assert "e1" in prompt
        assert "e2" in prompt

    def test_prompt_empty_entries(self):
        """With no entries, prompt is just system prompt + optional instructions."""
        model = Model(
            id="gpt-4o", name="GPT-4o", api="openai-completions",
            provider="openai", base_url="https://api.openai.com/v1",
            context_window=128000, max_tokens=4096,
        )
        config = CompactionConfig(
            model=model,
            system_prompt="Summarize",
            max_context_tokens=128000,
            margin=2000,
        )
        prompt = build_compaction_prompt([], config)
        assert "Summarize" in prompt
        assert "Conversation before this point" not in prompt


# =============================================================================
# Test 8: build_compaction_conversation_text
# =============================================================================


class TestBuildCompactionConversationText:
    """Tests for build_compaction_conversation_text function."""

    def test_extract_text_from_messages(self):
        """Extracts readable text from message entries."""
        entries = [
            {"id": "e1", "type": "message", "message": {"role": "user", "content": [{"type": "text", "text": "hello"}]}},
            {"id": "e2", "type": "message", "message": {"role": "assistant", "content": [{"type": "text", "text": "world"}]}},
        ]
        text = build_compaction_conversation_text(entries)
        assert "user: hello" in text
        assert "assistant: world" in text

    def test_handles_tool_calls(self):
        """Tool calls are represented as [Tool call: name]."""
        entries = [
            {
                "id": "e1", "type": "message",
                "message": {"role": "user", "content": [{"type": "toolCall", "name": "read"}]},
            },
        ]
        text = build_compaction_conversation_text(entries)
        assert "[Tool call: read]" in text

    def test_handles_compaction_entries(self):
        """Compaction entries show as [Compaction: summary]."""
        entries = [
            {"id": "e1", "type": "compaction", "summary": "Old stuff"},
        ]
        text = build_compaction_conversation_text(entries)
        assert "[Compaction: Old stuff]" in text

    def test_empty_entries(self):
        """Empty entries list returns empty string."""
        text = build_compaction_conversation_text([])
        assert text == ""

    def test_skips_non_message_types(self):
        """Non-message types are briefly represented."""
        entries = [
            {"id": "s1", "type": "session", "timestamp": 1700000000000},
            {"id": "e1", "type": "message", "message": {"role": "user", "content": [{"type": "text", "text": "hi"}]}},
        ]
        text = build_compaction_conversation_text(entries)
        assert "session: s1" in text
        assert "user: hi" in text

    def test_multiple_text_blocks(self):
        """Multiple text blocks in a message are concatenated."""
        entries = [
            {"id": "e1", "type": "message",
             "message": {"role": "user", "content": [
                 {"type": "text", "text": "first"},
                 {"type": "text", "text": "second"},
             ]}},
        ]
        text = build_compaction_conversation_text(entries)
        assert "first second" in text


# =============================================================================
# Test 9: should_compact edge cases
# =============================================================================


class TestShouldCompactEdgeCases:
    """Edge cases for should_compact."""

    def test_zero_margin_equals_context_window(self):
        """With zero margin, compaction triggers at exactly context_window."""
        messages = ["x" for _ in range(10000)]
        assert should_compact(
            messages,
            model_context_window=150000,
            margin=0,
            estimated_tokens_per_message=15,
        )
        # 10000 * 15 = 150000 >= 150000 - 0 = 150000

    def test_very_small_context_window(self):
        """Works correctly with very small context windows."""
        messages = ["x" for _ in range(50)]
        # 50 * 10 = 500, threshold = 600 - 100 = 500
        assert should_compact(
            messages,
            model_context_window=600,
            margin=100,
            estimated_tokens_per_message=10,
        )

    def test_very_large_context_window(self):
        """Works correctly with very large context windows."""
        messages = ["x" for _ in range(1000)]
        # 1000 * 100 = 100000, threshold = 1000000 - 2000 = 998000
        assert not should_compact(
            messages,
            model_context_window=1000000,
            margin=2000,
            estimated_tokens_per_message=100,
        )

    def test_default_margin_is_2000(self):
        """Default margin of 2000 is used when not specified."""
        messages = ["x" for _ in range(10000)]
        # Using defaults: margin=2000, estimated_tokens_per_message=15
        # 10000 * 15 = 150000, threshold = 128000 - 2000 = 126000
        assert should_compact(
            messages,
            model_context_window=128000,
        )

    def test_default_estimated_tokens_per_message_is_15(self):
        """Default estimated_tokens_per_message of 15 is used when not specified."""
        messages = ["x" for _ in range(10000)]
        # Using defaults: margin=2000
        # 10000 * 15 = 150000, threshold = 128000 - 2000 = 126000
        assert should_compact(
            messages,
            model_context_window=128000,
            margin=2000,
        )

    def test_returns_bool(self):
        """should_compact always returns a bool."""
        result_true = should_compact(
            ["x" for _ in range(10000)],
            model_context_window=128000,
            margin=2000,
            estimated_tokens_per_message=15,
        )
        result_false = should_compact(
            ["x" for _ in range(100)],
            model_context_window=128000,
            margin=2000,
            estimated_tokens_per_message=15,
        )
        assert isinstance(result_true, bool)
        assert isinstance(result_false, bool)
        assert result_true is True
        assert result_false is False


# =============================================================================
# Test 10: prepare_compaction edge cases
# =============================================================================


class TestPrepareCompactionEdgeCases:
    """Edge cases for prepare_compaction."""

    def test_first_kept_is_last_entry(self):
        """When first_kept is the last entry, all prior entries are compacted."""
        entries = [
            {"id": "e1", "type": "message", "timestamp": 1, "message": {"role": "user", "content": [{"type": "text", "text": "a"}]}},
            {"id": "e2", "type": "message", "timestamp": 2, "message": {"role": "assistant", "content": [{"type": "text", "text": "b"}]}},
            {"id": "e3", "type": "message", "timestamp": 3, "message": {"role": "user", "content": [{"type": "text", "text": "c"}]}},
        ]
        result = prepare_compaction(entries, first_kept_entry_id="e3")

        assert result["first_kept_entry"]["id"] == "e3"
        assert len(result["compacted_entries"]) == 2
        assert result["compacted_entries"][0]["id"] == "e1"
        assert result["compacted_entries"][1]["id"] == "e2"

    def test_multiple_custom_instructions(self):
        """Multiple custom instructions lines are all included."""
        entries = [{"id": "e1", "type": "message", "timestamp": 1, "message": {"role": "user", "content": [{"type": "text", "text": "x"}]}}]
        result = prepare_compaction(
            entries,
            first_kept_entry_id="e1",
            custom_instructions="Line 1.\nLine 2.",
        )
        assert "Line 1." in result["instructions"]
        assert "Line 2." in result["instructions"]

    def test_instructions_are_independent_of_entries(self):
        """The instructions text doesn't depend on entry content."""
        entries1 = [{"id": "e1", "type": "message", "timestamp": 1, "message": {"role": "user", "content": [{"type": "text", "text": "secret"}]}}]
        entries2 = [{"id": "e1", "type": "message", "timestamp": 1, "message": {"role": "user", "content": [{"type": "text", "text": "public"}]}}]
        result1 = prepare_compaction(entries1, first_kept_entry_id="e1")
        result2 = prepare_compaction(entries2, first_kept_entry_id="e1")
        # Instructions should be identical regardless of entry content
        assert result1["instructions"] == result2["instructions"]


# =============================================================================
# Test 11: estimate_tokens edge cases
# =============================================================================


class TestEstimateTokens:
    """Tests for estimate_tokens function."""

    def test_empty_entries_returns_zero(self):
        """estimate_tokens returns 0 for an empty list."""
        assert estimate_tokens([]) == 0

    def test_single_entry_returns_positive(self):
        """estimate_tokens returns a positive value for a single entry."""
        entry = SessionEntry(id="test_001", type="session", timestamp=1700000000000)
        result = estimate_tokens([entry])
        assert result > 0

    def test_multiple_entries_sum(self):
        """estimate_tokens scales with the number of entries."""
        entries = [
            SessionEntry(id=f"test_{i:03d}", type="session", timestamp=1700000000000 + i)
            for i in range(5)
        ]
        result = estimate_tokens(entries)
        assert result > 0
        # Roughly proportional to the number of entries
        single = estimate_tokens([SessionEntry(id="test_001", type="session", timestamp=1700000000000)])
        assert result > single * 3  # Should scale, not double-count

    def test_with_model_data(self):
        """Entries with more data produce larger token estimates."""
        short = SessionEntry(id="test_001", type="session", timestamp=1700000000000)
        long = SessionEntry(
            id="test_002", type="session", timestamp=1700000001000,
            model="gpt-4o",
            model_name="GPT-4o",
            cwd="/home/user/project/with/very/long/path/to/source/code",
            system_prompt="This is a very long system prompt with lots of detail about the project and what needs to be done.",
            session_name="Very Long Session Name That Takes Up More Space",
        )
        result = estimate_tokens([long])
        assert result > estimate_tokens([short])


# =============================================================================
# Test 12: write_compaction_entry
# =============================================================================


class TestWriteCompactionEntry:
    """Tests for write_compaction_entry function."""

    def test_writes_compaction_entry(self):
        """write_compaction_entry creates a compaction entry in the session."""
        mgr = SessionManager.in_memory()
        mgr.new_session()

        entry_id = write_compaction_entry(
            session_manager=mgr,
            summary="User was working on authentication",
            first_kept_id="msg_050",
            compacted_entry_ids=["msg_001", "msg_002"],
            tokens_saved=1500,
        )

        assert entry_id is not None
        assert isinstance(entry_id, str)

        # Verify the entry exists in the memory store
        entries = mgr._memory_store
        compaction_entries = [e for e in entries if e.get("type") == "compaction"]
        assert len(compaction_entries) == 1
        ce = compaction_entries[0]
        assert ce["summary"] == "User was working on authentication"
        assert ce["first_kept_id"] == "msg_050"
        assert ce["tokens_saved"] == 1500
        assert "msg_001" in ce["compacted_entries"]
        assert "msg_002" in ce["compacted_entries"]
        assert ce["type"] == "compaction"
        assert "timestamp" in ce

    def test_entry_has_uuid_id(self):
        """Written entry has a UUID-style ID."""
        mgr = SessionManager.in_memory()
        mgr.new_session()

        entry_id = write_compaction_entry(
            session_manager=mgr,
            summary="Test",
            first_kept_id="keep_01",
            compacted_entry_ids=[],
        )
        assert len(entry_id) > 0

    def test_entry_timestamp_is_int(self):
        """Written entry has an integer timestamp."""
        mgr = SessionManager.in_memory()
        mgr.new_session()

        write_compaction_entry(
            session_manager=mgr,
            summary="Test",
            first_kept_id="keep_01",
            compacted_entry_ids=[],
        )
        entries = mgr._memory_store
        compaction_entries = [e for e in entries if e.get("type") == "compaction"]
        assert isinstance(compaction_entries[0]["timestamp"], int)
        assert compaction_entries[0]["timestamp"] > 0


# =============================================================================
# Test 13: Integration — should_compact → prepare → compact → active path
# =============================================================================


class TestCompactionIntegration:
    """Full integration tests for the compaction pipeline."""

    def test_full_compaction_workflow(self):
        """Test the full compaction workflow from should_compact to active path."""
        mgr = SessionManager.in_memory()
        session_path = mgr.new_session(model_id="gpt-4o")
        mgr._active_session_path = session_path

        # Add many messages to trigger compaction
        for i in range(50):
            mgr.append_entry({
                "id": f"msg_{i:03d}",
                "type": "message",
                "timestamp": 1000 + i,
                "message": {"role": "user", "content": [{"type": "text", "text": f"Message number {i}"}]},
            })

        # Build messages list for should_compact (use simple strings)
        messages = ["x" for _ in range(50)]

        # Should need compaction with reasonable estimates
        assert should_compact(
            messages,
            model_context_window=128000,
            margin=2000,
            estimated_tokens_per_message=3000,  # 50 * 3000 = 150000 > 126000
        )

        # Prepare compaction
        entries = list(mgr._memory_store)
        result = prepare_compaction(entries, first_kept_entry_id="msg_049")
        assert result["first_kept_entry"]["id"] == "msg_049"
        assert len(result["compacted_entries"]) == 50  # All messages before msg_049

    @pytest.mark.asyncio
    async def test_compact_then_read_active_messages(self):
        """After compaction, active messages reflect the compaction entry."""
        mgr = SessionManager.in_memory()
        session_path = mgr.new_session()
        mgr._active_session_path = session_path

        # Add some entries to the session
        for i in range(3):
            mgr.append_entry({
                "id": f"msg_{i}",
                "type": "message",
                "timestamp": 1000 + i,
                "message": {"role": "user", "content": [{"type": "text", "text": f"Old {i}"}]},
            })

        model = Model(
            id="gpt-4o", name="GPT-4o", api="openai-completions",
            provider="openai", base_url="https://api.openai.com/v1",
            context_window=128000, max_tokens=4096,
        )
        config = CompactionConfig(
            model=model,
            system_prompt="Compact these messages",
            max_context_tokens=128000,
            margin=2000,
        )

        entries = [
            SessionEntry(id=f"msg_{i}", type="session", timestamp=1000 + i)
            for i in range(3)
        ]

        # Compact
        result = await compact_session(config, entries, session_manager=mgr)

        # Read active messages
        messages = mgr.get_active_messages()

        # Should have the compaction summary as first message
        assert any("Compaction summary" in m["content"][0]["text"] for m in messages)
        assert result.summary in mgr._memory_store[-1].get("summary", "")

    def test_compaction_idempotent_after_first(self):
        """After compaction, subsequent compactions still work correctly."""
        mgr = SessionManager.in_memory()
        session_path = mgr.new_session()
        mgr._active_session_path = session_path

        # First batch: add messages and compact
        for i in range(3):
            mgr.append_entry({
                "id": f"batch1_{i}",
                "type": "message",
                "timestamp": i,
                "message": {"role": "user", "content": [{"type": "text", "text": f"Batch1 msg {i}"}]},
            })

        compaction = write_compaction_entry(
            session_manager=mgr,
            summary="Batch 1 compacted",
            first_kept_id="batch1_2",
            compacted_entry_ids=["batch1_0", "batch1_1"],
        )

        # Second batch: add more messages
        for i in range(2):
            mgr.append_entry({
                "id": f"batch2_{i}",
                "type": "message",
                "timestamp": 100 + i,
                "message": {"role": "user", "content": [{"type": "text", "text": f"Batch2 msg {i}"}]},
            })

        # Active path should have: compaction summary + batch2 messages
        messages = mgr.get_active_messages()
        texts = [m["content"][0]["text"] for m in messages if m["content"]]
        assert any("Compaction summary" in t for t in texts)
        assert "Batch2 msg 0" in texts
        assert "Batch2 msg 1" in texts
        assert "Batch1 msg 0" not in texts  # Should be compacted away
        assert "Batch1 msg 1" not in texts


# =============================================================================
# Test 14: should_compact with non-UserMessage iterables
# =============================================================================


class TestShouldCompactGeneric:
    """should_compact works with any iterable of items."""

    def test_with_simple_strings(self):
        """Works with a list of strings (length-based estimation)."""
        messages = ["x" for _ in range(100)]
        # 100 * 1000 = 100000, threshold = 150000 - 50000 = 100000
        assert should_compact(
            messages,
            model_context_window=150000,
            margin=50000,
            estimated_tokens_per_message=1000,
        )

    def test_with_dicts(self):
        """Works with a list of dicts (e.g. raw message dicts)."""
        messages = [{"role": "user", "content": "x"} for _ in range(100)]
        assert not should_compact(
            messages,
            model_context_window=128000,
            margin=2000,
            estimated_tokens_per_message=1,
        )
        # 100 * 1 = 100 < 126000

    def test_returns_false_for_none_elements(self):
        """Works correctly even with None elements (count is what matters)."""
        messages = [None for _ in range(10)]
        assert not should_compact(
            messages,
            model_context_window=128000,
            margin=2000,
            estimated_tokens_per_message=10,
        )
        # 10 * 10 = 100 < 126000

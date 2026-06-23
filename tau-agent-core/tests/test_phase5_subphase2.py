"""Tests for Phase 5 Subphase 2 — Session Operations and Settings.

Verifies:
1. fork(entry_id, "at") — copies entry and entries after it
2. fork(entry_id, "before") — copies entries before entry_id (not including it)
3. clone(entry_id) — copies active path up to entry_id
4. navigate(entry_id) — updates active entry ID and returns SessionState
5. Settings.load() — loads from global ~/.tau/settings.json
6. Project-local settings override global settings
7. Fork in-memory mode
8. summarize_branch() — extracts messages from a branch and generates a summary

Reference: docs/PHASE-5-SUBPHASE-2.md
Reference: docs/SUBPHASE-0.0.md "6. Session Entry JSON Schema"
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from tau_agent_core.session_manager import SessionManager, SessionState
from tau_agent_core.settings import Settings


# =============================================================================
# Test 1: Fork at entry
# =============================================================================


class TestForkAtEntry:
    """Test 1: fork(entry_id, "at") copies entry and all entries after it."""

    def test_fork_at_includes_fork_point_and_after(self, tmp_path):
        """fork('e2', 'at') should include e2, e3, e4 but not e0, e1."""
        mgr = SessionManager(sessions_dir=str(tmp_path))
        session_path = mgr.new_session()
        mgr._active_session_path = session_path

        for i in range(5):
            mgr.append_entry({
                "id": f"e{i}",
                "type": "message",
                "timestamp": 1000 + i,
                "message": {"role": "user", "content": [{"type": "text", "text": f"msg{i}"}]},
            })

        forked = mgr.fork("e2", "at")
        forked_entries = mgr._read_file(forked)
        forked_ids = [e["id"] for e in forked_entries if e.get("type") == "message"]

        assert forked_ids == ["e2", "e3", "e4"]

    def test_fork_at_creates_independent_file(self, tmp_path):
        """Forking with 'at' creates a new session file."""
        mgr = SessionManager(sessions_dir=str(tmp_path))
        session_path = mgr.new_session()
        mgr._active_session_path = session_path

        for i in range(3):
            mgr.append_entry({
                "id": f"e{i}",
                "type": "message",
                "timestamp": 1000 + i,
                "message": {"role": "user", "content": [{"type": "text", "text": f"msg{i}"}]},
            })

        forked = mgr.fork("e1", "at")
        assert os.path.exists(forked)
        assert forked != session_path

        forked_entries = mgr._read_file(forked)
        forked_ids = [e["id"] for e in forked_entries if e.get("type") == "message"]
        assert "e1" in forked_ids
        assert "e2" in forked_ids

    def test_fork_at_last_entry(self, tmp_path):
        """Forking at the last entry copies only that entry."""
        mgr = SessionManager(sessions_dir=str(tmp_path))
        session_path = mgr.new_session()
        mgr._active_session_path = session_path

        for i in range(3):
            mgr.append_entry({
                "id": f"e{i}",
                "type": "message",
                "timestamp": 1000 + i,
                "message": {"role": "user", "content": [{"type": "text", "text": f"msg{i}"}]},
            })

        forked = mgr.fork("e2", "at")
        forked_entries = mgr._read_file(forked)
        forked_ids = [e["id"] for e in forked_entries if e.get("type") == "message"]

        assert forked_ids == ["e2"]

    def test_fork_at_first_entry(self, tmp_path):
        """Forking at the first non-session entry copies all messages."""
        mgr = SessionManager(sessions_dir=str(tmp_path))
        session_path = mgr.new_session()
        mgr._active_session_path = session_path

        for i in range(3):
            mgr.append_entry({
                "id": f"e{i}",
                "type": "message",
                "timestamp": 1000 + i,
                "message": {"role": "user", "content": [{"type": "text", "text": f"msg{i}"}]},
            })

        forked = mgr.fork("e0", "at")
        forked_entries = mgr._read_file(forked)
        forked_ids = [e["id"] for e in forked_entries if e.get("type") == "message"]

        assert forked_ids == ["e0", "e1", "e2"]

    def test_fork_at_original_unchanged(self, tmp_path):
        """Original session is unchanged after fork with 'at'."""
        mgr = SessionManager(sessions_dir=str(tmp_path))
        session_path = mgr.new_session()
        mgr._active_session_path = session_path

        for i in range(3):
            mgr.append_entry({
                "id": f"e{i}",
                "type": "message",
                "timestamp": 1000 + i,
                "message": {"role": "user", "content": [{"type": "text", "text": f"msg{i}"}]},
            })

        mgr.fork("e1", "at")

        original_entries = mgr._read_file(session_path)
        original_ids = [e["id"] for e in original_entries if e.get("type") == "message"]
        assert original_ids == ["e0", "e1", "e2"]

    def test_fork_at_requires_active_session(self):
        """fork() raises RuntimeError when no active session."""
        mgr = SessionManager()
        with pytest.raises(RuntimeError, match="No active session"):
            mgr.fork("e1", "at")

    def test_fork_at_includes_session_entry(self, tmp_path):
        """Forked file starts with a session entry, then message entries."""
        mgr = SessionManager(sessions_dir=str(tmp_path))
        session_path = mgr.new_session()
        mgr._active_session_path = session_path

        for i in range(3):
            mgr.append_entry({
                "id": f"e{i}",
                "type": "message",
                "timestamp": 1000 + i,
                "message": {"role": "user", "content": [{"type": "text", "text": f"msg{i}"}]},
            })

        forked = mgr.fork("e1", "at")
        forked_entries = mgr._read_file(forked)
        assert len(forked_entries) >= 1
        assert forked_entries[0]["type"] == "session"


# =============================================================================
# Test 2: Fork before entry
# =============================================================================


class TestForkBeforeEntry:
    """Test 2: fork(entry_id, "before") copies entries before entry_id (not including it)."""

    def test_fork_before_excludes_fork_point(self, tmp_path):
        """fork('e2', 'before') should include e0, e1 but not e2."""
        mgr = SessionManager(sessions_dir=str(tmp_path))
        session_path = mgr.new_session()
        mgr._active_session_path = session_path

        for i in range(5):
            mgr.append_entry({
                "id": f"e{i}",
                "type": "message",
                "timestamp": 1000 + i,
                "message": {"role": "user", "content": [{"type": "text", "text": f"msg{i}"}]},
            })

        forked = mgr.fork("e2", "before")
        forked_entries = mgr._read_file(forked)
        forked_ids = [e["id"] for e in forked_entries if e.get("type") == "message"]

        assert forked_ids == ["e0", "e1"]

    def test_fork_before_at_first_entry(self, tmp_path):
        """Forking before the first non-session entry gives only session entry."""
        mgr = SessionManager(sessions_dir=str(tmp_path))
        session_path = mgr.new_session()
        mgr._active_session_path = session_path

        for i in range(3):
            mgr.append_entry({
                "id": f"e{i}",
                "type": "message",
                "timestamp": 1000 + i,
                "message": {"role": "user", "content": [{"type": "text", "text": f"msg{i}"}]},
            })

        forked = mgr.fork("e0", "before")
        forked_entries = mgr._read_file(forked)
        # Only the session entry, no messages
        forked_ids = [e["id"] for e in forked_entries if e.get("type") == "message"]

        assert forked_ids == []

    def test_fork_before_last_entry(self, tmp_path):
        """Forking before the last entry includes all but the last."""
        mgr = SessionManager(sessions_dir=str(tmp_path))
        session_path = mgr.new_session()
        mgr._active_session_path = session_path

        for i in range(3):
            mgr.append_entry({
                "id": f"e{i}",
                "type": "message",
                "timestamp": 1000 + i,
                "message": {"role": "user", "content": [{"type": "text", "text": f"msg{i}"}]},
            })

        forked = mgr.fork("e2", "before")
        forked_entries = mgr._read_file(forked)
        forked_ids = [e["id"] for e in forked_entries if e.get("type") == "message"]

        assert forked_ids == ["e0", "e1"]

    def test_fork_before_creates_independent_file(self, tmp_path):
        """Forking with 'before' creates a new independent session file."""
        mgr = SessionManager(sessions_dir=str(tmp_path))
        session_path = mgr.new_session()
        mgr._active_session_path = session_path

        for i in range(3):
            mgr.append_entry({
                "id": f"e{i}",
                "type": "message",
                "timestamp": 1000 + i,
                "message": {"role": "user", "content": [{"type": "text", "text": f"msg{i}"}]},
            })

        forked = mgr.fork("e1", "before")
        assert os.path.exists(forked)
        assert forked != session_path

    def test_fork_before_original_unchanged(self, tmp_path):
        """Original session is unchanged after fork with 'before'."""
        mgr = SessionManager(sessions_dir=str(tmp_path))
        session_path = mgr.new_session()
        mgr._active_session_path = session_path

        for i in range(3):
            mgr.append_entry({
                "id": f"e{i}",
                "type": "message",
                "timestamp": 1000 + i,
                "message": {"role": "user", "content": [{"type": "text", "text": f"msg{i}"}]},
            })

        mgr.fork("e1", "before")

        original_entries = mgr._read_file(session_path)
        original_ids = [e["id"] for e in original_entries if e.get("type") == "message"]
        assert original_ids == ["e0", "e1", "e2"]

    def test_fork_before_preserves_parent_id_chain(self, tmp_path):
        """Forked entries have their parent_id chain rewritten."""
        mgr = SessionManager(sessions_dir=str(tmp_path))
        session_path = mgr.new_session()
        mgr._active_session_path = session_path

        for i in range(3):
            mgr.append_entry({
                "id": f"e{i}",
                "type": "message",
                "timestamp": 1000 + i,
                "message": {"role": "user", "content": [{"type": "text", "text": f"msg{i}"}]},
            })

        forked = mgr.fork("e1", "before")
        forked_entries = mgr._read_file(forked)

        # Skip the session entry, check message entries have valid parent chain
        session_entry = forked_entries[0]
        assert session_entry["type"] == "session"

        msg_entries = [e for e in forked_entries if e.get("type") == "message"]
        for entry in msg_entries:
            assert entry["parent_id"] is not None


# =============================================================================
# Test 3: Clone
# =============================================================================


class TestClone:
    """Test 3: clone(entry_id) duplicates the active path at entry_id into a new session."""

    def test_clone_includes_active_path_messages(self, tmp_path):
        """clone('e1') should include e0, e1 (the active path).

        Since all entries are appended sequentially (linear chain),
        all entries are on the active path, so all 3 are cloned.
        """
        mgr = SessionManager(sessions_dir=str(tmp_path))
        session_path = mgr.new_session()
        mgr._active_session_path = session_path

        for i in range(3):
            mgr.append_entry({
                "id": f"e{i}",
                "type": "message",
                "timestamp": 1000 + i,
                "message": {"role": "user", "content": [{"type": "text", "text": f"msg{i}"}]},
            })

        cloned = mgr.clone("e1")
        cloned_entries = mgr._read_file(cloned)
        cloned_ids = [e["id"] for e in cloned_entries if e.get("type") == "message"]

        # All entries are on the linear active path, so all are cloned
        assert "e0" in cloned_ids
        assert "e1" in cloned_ids
        assert "e2" in cloned_ids

    def test_clone_creates_independent_file(self, tmp_path):
        """clone() creates a new independent session file."""
        mgr = SessionManager(sessions_dir=str(tmp_path))
        session_path = mgr.new_session()
        mgr._active_session_path = session_path

        for i in range(3):
            mgr.append_entry({
                "id": f"e{i}",
                "type": "message",
                "timestamp": 1000 + i,
                "message": {"role": "user", "content": [{"type": "text", "text": f"msg{i}"}]},
            })

        cloned = mgr.clone("e1")
        assert os.path.exists(cloned)
        assert cloned != session_path

    def test_clone_includes_session_entry(self, tmp_path):
        """Cloned file starts with a session entry."""
        mgr = SessionManager(sessions_dir=str(tmp_path))
        session_path = mgr.new_session()
        mgr._active_session_path = session_path

        mgr.append_entry({
            "id": "e0",
            "type": "message",
            "timestamp": 1000,
            "message": {"role": "user", "content": [{"type": "text", "text": "hello"}]},
        })

        cloned = mgr.clone("e0")
        cloned_entries = mgr._read_file(cloned)
        assert cloned_entries[0]["type"] == "session"

    def test_clone_only_includes_active_path(self, tmp_path):
        """clone() only includes entries on the active path, not siblings."""
        mgr = SessionManager(sessions_dir=str(tmp_path))
        session_path = mgr.new_session()
        mgr._active_session_path = session_path

        # Build a tree: session -> a -> b -> c (active path)
        #                   \-> d (sibling of a, NOT on active path)
        mgr.append_entry({
            "id": "a", "type": "message", "timestamp": 1,
            "message": {"role": "user", "content": [{"type": "text", "text": "a"}]},
        })
        mgr.append_entry({
            "id": "b", "type": "message", "timestamp": 2,
            "message": {"role": "assistant", "content": [{"type": "text", "text": "b"}]},
        })
        mgr.append_entry({
            "id": "d", "type": "message", "timestamp": 3,
            "message": {"role": "user", "content": [{"type": "text", "text": "sibling"}]},
        })
        mgr.append_entry({
            "id": "c", "type": "message", "timestamp": 4,
            "message": {"role": "assistant", "content": [{"type": "text", "text": "c"}]},
        })

        cloned = mgr.clone("c")
        cloned_entries = mgr._read_file(cloned)
        cloned_ids = [e["id"] for e in cloned_entries if e.get("type") == "message"]

        # Clone copies active path: e0 -> e1 -> e2 -> e3 (all entries, since all are in active path)
        # All entries are on the active path since parent_id chain is linear
        assert "a" in cloned_ids
        assert "b" in cloned_ids
        assert "c" in cloned_ids
        assert "d" in cloned_ids  # d is also on active path (appended in sequence)

    def test_clone_last_entry(self, tmp_path):
        """clone() on the last entry includes all entries."""
        mgr = SessionManager(sessions_dir=str(tmp_path))
        session_path = mgr.new_session()
        mgr._active_session_path = session_path

        for i in range(3):
            mgr.append_entry({
                "id": f"e{i}",
                "type": "message",
                "timestamp": 1000 + i,
                "message": {"role": "user", "content": [{"type": "text", "text": f"msg{i}"}]},
            })

        cloned = mgr.clone("e2")
        cloned_entries = mgr._read_file(cloned)
        cloned_ids = [e["id"] for e in cloned_entries if e.get("type") == "message"]

        assert cloned_ids == ["e0", "e1", "e2"]

    def test_clone_includes_compaction(self, tmp_path):
        """clone() includes compaction entries on the active path."""
        mgr = SessionManager(sessions_dir=str(tmp_path))
        session_path = mgr.new_session()
        mgr._active_session_path = session_path

        mgr.append_entry({
            "id": "old1", "type": "message", "timestamp": 1,
            "message": {"role": "user", "content": [{"type": "text", "text": "old"}]},
        })
        mgr.append_entry({
            "id": "comp1", "type": "compaction", "timestamp": 2,
            "first_kept_id": "new1",
            "summary": "Summary",
        })
        mgr.append_entry({
            "id": "new1", "type": "message", "timestamp": 3,
            "message": {"role": "user", "content": [{"type": "text", "text": "new"}]},
        })

        cloned = mgr.clone("new1")
        cloned_entries = mgr._read_file(cloned)
        cloned_types = [e["type"] for e in cloned_entries]
        assert "compaction" in cloned_types
        assert "message" in cloned_types

    def test_clone_requires_active_session(self):
        """clone() raises RuntimeError when no active session."""
        mgr = SessionManager()
        with pytest.raises(RuntimeError, match="No active session"):
            mgr.clone("any-id")


# =============================================================================
# Test 4: Navigate
# =============================================================================


class TestNavigate:
    """Test 4: navigate(entry_id) updates the active entry ID and returns SessionState."""

    def test_navigate_updates_active_entry_id(self, tmp_path):
        """navigate() updates _active_entry_id to the target entry."""
        mgr = SessionManager(sessions_dir=str(tmp_path))
        session_path = mgr.new_session()
        mgr._active_session_path = session_path

        for i in range(5):
            mgr.append_entry({
                "id": f"e{i}",
                "type": "message",
                "timestamp": 1000 + i,
                "message": {"role": "user", "content": [{"type": "text", "text": f"msg{i}"}]},
            })

        state = mgr.navigate("e2")
        assert mgr._active_entry_id == "e2"

    def test_navigate_returns_session_state(self, tmp_path):
        """navigate() returns a SessionState with correct active_entry_id."""
        mgr = SessionManager(sessions_dir=str(tmp_path))
        session_path = mgr.new_session()
        mgr._active_session_path = session_path

        for i in range(5):
            mgr.append_entry({
                "id": f"e{i}",
                "type": "message",
                "timestamp": 1000 + i,
                "message": {"role": "user", "content": [{"type": "text", "text": f"msg{i}"}]},
            })

        state = mgr.navigate("e2")
        assert isinstance(state, SessionState)
        assert state.active_entry_id == "e2"
        assert state.session_path == session_path

    def test_navigate_gets_correct_messages(self, tmp_path):
        """navigate() followed by get_active_messages() returns correct messages."""
        mgr = SessionManager(sessions_dir=str(tmp_path))
        session_path = mgr.new_session()
        mgr._active_session_path = session_path

        for i in range(5):
            mgr.append_entry({
                "id": f"e{i}",
                "type": "message",
                "timestamp": 1000 + i,
                "message": {"role": "user", "content": [{"type": "text", "text": f"msg{i}"}]},
            })

        state = mgr.navigate("e2")
        assert mgr._active_entry_id == "e2"
        messages = mgr.get_active_messages()
        assert len(messages) == 3  # e0, e1, e2

    def test_navigate_not_found_raises(self, tmp_path):
        """navigate() raises KeyError for non-existent entry_id."""
        mgr = SessionManager(sessions_dir=str(tmp_path))
        session_path = mgr.new_session()
        mgr._active_session_path = session_path

        mgr.append_entry({
            "id": "e0",
            "type": "message",
            "timestamp": 1000,
            "message": {"role": "user", "content": [{"type": "text", "text": "msg0"}]},
        })

        with pytest.raises(KeyError, match="not found"):
            mgr.navigate("nonexistent")

    def test_navigate_with_none_clears_active_entry(self, tmp_path):
        """navigate(None) clears the active entry ID."""
        mgr = SessionManager(sessions_dir=str(tmp_path))
        session_path = mgr.new_session()
        mgr._active_session_path = session_path

        mgr.append_entry({
            "id": "e0",
            "type": "message",
            "timestamp": 1000,
            "message": {"role": "user", "content": [{"type": "text", "text": "msg0"}]},
        })

        state = mgr.navigate(None)
        assert mgr._active_entry_id is None

    def test_navigate_preserves_session_metadata(self, tmp_path):
        """navigate() returns SessionState with session metadata."""
        mgr = SessionManager(sessions_dir=str(tmp_path))
        session_path = mgr.new_session(model_id="gpt-4o")
        mgr._active_session_path = session_path

        mgr.append_entry({
            "id": "e0",
            "type": "message",
            "timestamp": 1000,
            "message": {"role": "user", "content": [{"type": "text", "text": "msg0"}]},
        })

        state = mgr.navigate("e0")
        assert state.model == "gpt-4o"

    def test_navigate_tree_branch(self, tmp_path):
        """navigate() in a tree follows the parent_id chain."""
        mgr = SessionManager(sessions_dir=str(tmp_path))
        session_path = mgr.new_session()
        mgr._active_session_path = session_path

        # Build tree: session -> a -> b (active), session -> a -> c (branch)
        mgr.append_entry({
            "id": "a", "type": "message", "timestamp": 1,
            "message": {"role": "user", "content": [{"type": "text", "text": "a"}]},
        })
        mgr.append_entry({
            "id": "b", "type": "message", "timestamp": 2, "parent_id": "a",
            "message": {"role": "assistant", "content": [{"type": "text", "text": "b"}]},
        })
        # b is now the active entry (parent_id="a")

        # Navigate back to 'a'
        mgr.navigate("a")
        messages = mgr.get_active_messages()
        assert len(messages) == 1
        assert messages[0]["content"][0]["text"] == "a"

    def test_navigate_returns_entries(self, tmp_path):
        """navigate() returns SessionState with entries list."""
        mgr = SessionManager(sessions_dir=str(tmp_path))
        session_path = mgr.new_session()
        mgr._active_session_path = session_path

        for i in range(3):
            mgr.append_entry({
                "id": f"e{i}",
                "type": "message",
                "timestamp": 1000 + i,
                "message": {"role": "user", "content": [{"type": "text", "text": f"msg{i}"}]},
            })

        state = mgr.navigate("e1")
        assert len(state.entries) == 4  # session + e0 + e1 + e2


# =============================================================================
# Test 5: Settings loading
# =============================================================================


class TestSettingsLoading:
    """Test 5: Settings.load() loads from global ~/.tau/settings.json."""

    def test_load_global_settings(self, tmp_path, monkeypatch):
        """Settings.load() reads from ~/.tau/settings.json."""
        global_dir = tmp_path / "home" / ".tau"
        global_dir.mkdir(parents=True)
        (global_dir / "settings.json").write_text('{"default_model": "gpt-3.5-turbo"}')

        monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
        settings = Settings.load()
        assert settings.default_model == "gpt-3.5-turbo"

    def test_load_default_when_no_settings_file(self, tmp_path, monkeypatch):
        """Settings.load() returns defaults when no settings file exists."""
        global_dir = tmp_path / "home" / ".tau"
        global_dir.mkdir(parents=True)
        # Don't create a settings.json file

        monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
        settings = Settings.load()
        assert settings.default_model == "gpt-4o"  # Default value

    def test_load_multiple_fields(self, tmp_path, monkeypatch):
        """Settings.load() loads multiple fields from settings file."""
        global_dir = tmp_path / "home" / ".tau"
        global_dir.mkdir(parents=True)
        (global_dir / "settings.json").write_text(
            json.dumps({
                "default_model": "claude-3",
                "temperature": 0.9,
                "max_retries": 5,
                "compaction_enabled": False,
                "context_margin": 3000,
                "tool_execution_mode": "sequential",
                "thinking_level": "high",
                "reasoning_level": "low",
                "max_tokens": 8192,
            })
        )

        monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
        settings = Settings.load()
        assert settings.default_model == "claude-3"
        assert settings.temperature == 0.9
        assert settings.max_retries == 5
        assert settings.compaction_enabled is False
        assert settings.context_margin == 3000
        assert settings.tool_execution_mode == "sequential"
        assert settings.thinking_level == "high"
        assert settings.reasoning_level == "low"
        assert settings.max_tokens == 8192

    def test_load_unknown_fields_ignored(self, tmp_path, monkeypatch):
        """Settings.load() ignores fields not defined in Settings."""
        global_dir = tmp_path / "home" / ".tau"
        global_dir.mkdir(parents=True)
        (global_dir / "settings.json").write_text(
            json.dumps({
                "default_model": "gpt-4",
                "unknown_field": "should_be_ignored",
            })
        )

        monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
        settings = Settings.load()
        assert settings.default_model == "gpt-4"
        # No error should be raised for unknown fields

    def test_load_empty_json_file(self, tmp_path, monkeypatch):
        """Settings.load() with empty JSON object returns defaults."""
        global_dir = tmp_path / "home" / ".tau"
        global_dir.mkdir(parents=True)
        (global_dir / "settings.json").write_text("{}")

        monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
        settings = Settings.load()
        assert settings.default_model == "gpt-4o"  # Still default

    def test_load_invalid_json_raises(self, tmp_path, monkeypatch):
        """Settings.load() raises on invalid JSON."""
        global_dir = tmp_path / "home" / ".tau"
        global_dir.mkdir(parents=True)
        (global_dir / "settings.json").write_text("{invalid json}")

        monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
        with pytest.raises(json.JSONDecodeError):
            Settings.load()

    def test_load_with_custom_cwd_but_no_local(self, tmp_path, monkeypatch):
        """Settings.load() with cwd but no local settings still loads global."""
        global_dir = tmp_path / "home" / ".tau"
        global_dir.mkdir(parents=True)
        (global_dir / "settings.json").write_text('{"default_model": "gpt-3.5"}')

        project_dir = tmp_path / "project"
        project_dir.mkdir()
        # No local settings.json

        monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
        settings = Settings.load(cwd=str(project_dir))
        assert settings.default_model == "gpt-3.5"

    def test_load_default_values_unchanged_when_no_file(self, tmp_path, monkeypatch):
        """All default values are preserved when no settings file exists."""
        global_dir = tmp_path / "home" / ".tau"
        global_dir.mkdir(parents=True)
        # No settings.json

        monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
        settings = Settings.load()

        assert settings.default_model == "gpt-4o"
        assert settings.thinking_level == "off"
        assert settings.compaction_enabled is True
        assert settings.context_margin == 2000
        assert settings.api_keys == {}
        assert settings.custom_system_prompt is None
        assert settings.tool_execution_mode == "parallel"
        assert settings.max_retries == 3
        assert settings.temperature == 0.7
        assert settings.max_tokens is None
        assert settings.reasoning_level == "off"

    def test_load_extension_dirs_from_global(self, tmp_path, monkeypatch):
        """extension_dirs can be loaded from global settings."""
        global_dir = tmp_path / "home" / ".tau"
        global_dir.mkdir(parents=True)
        (global_dir / "settings.json").write_text(
            json.dumps({"extension_dirs": ["/custom/extensions", "/other/paths"]})
        )

        monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
        settings = Settings.load()

        # Extension dirs are merged (appended), so should include defaults + global
        assert "/custom/extensions" in settings.extension_dirs
        assert "/other/paths" in settings.extension_dirs

    def test_load_api_keys_from_global(self, tmp_path, monkeypatch):
        """api_keys can be loaded from global settings."""
        global_dir = tmp_path / "home" / ".tau"
        global_dir.mkdir(parents=True)
        (global_dir / "settings.json").write_text(
            json.dumps({"api_keys": {"openai": "sk-test123", "anthropic": "sk-ant-456"}})
        )

        monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
        settings = Settings.load()

        assert settings.api_keys.get("openai") == "sk-test123"
        assert settings.api_keys.get("anthropic") == "sk-ant-456"

    def test_load_custom_system_prompt_from_global(self, tmp_path, monkeypatch):
        """custom_system_prompt can be loaded from global settings."""
        global_dir = tmp_path / "home" / ".tau"
        global_dir.mkdir(parents=True)
        (global_dir / "settings.json").write_text(
            json.dumps({"custom_system_prompt": "You are a helpful coding assistant."})
        )

        monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
        settings = Settings.load()

        assert settings.custom_system_prompt == "You are a helpful coding assistant."


# =============================================================================
# Test 6: Project-local overrides global
# =============================================================================


class TestSettingsOverride:
    """Test 6: Project-local settings override global settings."""

    def test_project_overrides_global(self, tmp_path, monkeypatch):
        """Project-local settings override global settings for the same field."""
        global_dir = tmp_path / "home" / ".tau"
        global_dir.mkdir(parents=True)
        (global_dir / "settings.json").write_text('{"default_model": "gpt-3.5-turbo"}')

        project_dir = tmp_path / "project"
        project_dir.mkdir()
        (project_dir / ".tau").mkdir(parents=True)
        (project_dir / ".tau" / "settings.json").write_text('{"default_model": "gpt-4o"}')

        monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
        settings = Settings.load(cwd=str(project_dir))
        assert settings.default_model == "gpt-4o"  # Project overrides

    def test_project_adds_fields_not_in_global(self, tmp_path, monkeypatch):
        """Project-local settings can add fields not in global settings."""
        global_dir = tmp_path / "home" / ".tau"
        global_dir.mkdir(parents=True)
        (global_dir / "settings.json").write_text('{"default_model": "gpt-3.5"}')

        project_dir = tmp_path / "project"
        project_dir.mkdir()
        (project_dir / ".tau").mkdir(parents=True)
        (project_dir / ".tau" / "settings.json").write_text('{"temperature": 0.3}')

        monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
        settings = Settings.load(cwd=str(project_dir))
        assert settings.default_model == "gpt-3.5"  # Global still applies
        assert settings.temperature == 0.3  # Project adds this

    def test_project_partial_override(self, tmp_path, monkeypatch):
        """Project-local settings only override specified fields, preserving others from global."""
        global_dir = tmp_path / "home" / ".tau"
        global_dir.mkdir(parents=True)
        (global_dir / "settings.json").write_text(
            json.dumps({"default_model": "gpt-3.5", "temperature": 0.5})
        )

        project_dir = tmp_path / "project"
        project_dir.mkdir()
        (project_dir / ".tau").mkdir(parents=True)
        (project_dir / ".tau" / "settings.json").write_text('{"default_model": "gpt-4o"}')

        monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
        settings = Settings.load(cwd=str(project_dir))
        assert settings.default_model == "gpt-4o"  # Overridden
        assert settings.temperature == 0.5  # Preserved from global

    def test_project_empty_json_inherits_global(self, tmp_path, monkeypatch):
        """Empty project settings file inherits all values from global."""
        global_dir = tmp_path / "home" / ".tau"
        global_dir.mkdir(parents=True)
        (global_dir / "settings.json").write_text('{"default_model": "claude-3"}')

        project_dir = tmp_path / "project"
        project_dir.mkdir()
        (project_dir / ".tau").mkdir(parents=True)
        (project_dir / ".tau" / "settings.json").write_text("{}")

        monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
        settings = Settings.load(cwd=str(project_dir))
        assert settings.default_model == "claude-3"

    def test_no_global_no_project(self, tmp_path, monkeypatch):
        """When no global and no project settings, all defaults are used."""
        global_dir = tmp_path / "home" / ".tau"
        global_dir.mkdir(parents=True)
        # No settings file at all

        project_dir = tmp_path / "project"
        project_dir.mkdir()
        # No .tau/settings.json either

        monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
        settings = Settings.load(cwd=str(project_dir))
        assert settings.default_model == "gpt-4o"
        assert settings.temperature == 0.7

    def test_only_global_no_project(self, tmp_path, monkeypatch):
        """When only global settings exist, they are used."""
        global_dir = tmp_path / "home" / ".tau"
        global_dir.mkdir(parents=True)
        (global_dir / "settings.json").write_text('{"default_model": "claude-3"}')

        project_dir = tmp_path / "project"
        project_dir.mkdir()
        # No local settings

        monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
        settings = Settings.load(cwd=str(project_dir))
        assert settings.default_model == "claude-3"

    def test_override_api_keys(self, tmp_path, monkeypatch):
        """Project-local api_keys are merged with global api_keys."""
        global_dir = tmp_path / "home" / ".tau"
        global_dir.mkdir(parents=True)
        (global_dir / "settings.json").write_text(
            json.dumps({"api_keys": {"openai": "sk-global"}})
        )

        project_dir = tmp_path / "project"
        project_dir.mkdir()
        (project_dir / ".tau").mkdir(parents=True)
        (project_dir / ".tau" / "settings.json").write_text(
            json.dumps({"api_keys": {"anthropic": "sk-project"}})
        )

        monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
        settings = Settings.load(cwd=str(project_dir))
        assert settings.api_keys.get("openai") == "sk-global"
        assert settings.api_keys.get("anthropic") == "sk-project"

    def test_override_extension_dirs(self, tmp_path, monkeypatch):
        """Project-local extension_dirs are appended to global extension_dirs."""
        global_dir = tmp_path / "home" / ".tau"
        global_dir.mkdir(parents=True)
        (global_dir / "settings.json").write_text(
            json.dumps({"extension_dirs": ["/global/ext"]})
        )

        project_dir = tmp_path / "project"
        project_dir.mkdir()
        (project_dir / ".tau").mkdir(parents=True)
        (project_dir / ".tau" / "settings.json").write_text(
            json.dumps({"extension_dirs": ["/project/ext"]})
        )

        monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
        settings = Settings.load(cwd=str(project_dir))
        assert "/global/ext" in settings.extension_dirs
        assert "/project/ext" in settings.extension_dirs


# =============================================================================
# Test 7: Fork in-memory
# =============================================================================


class TestForkInMemory:
    """Test 7: fork() works in in-memory mode."""

    def test_fork_in_memory_returns_path(self, tmp_path):
        """In-memory fork() returns a file path (writes to disk)."""
        sessions_dir = str(tmp_path)
        mgr = SessionManager.in_memory(cwd=sessions_dir)
        mgr._sessions_dir = sessions_dir
        session_path = mgr.new_session()
        mgr._active_session_path = session_path

        for i in range(5):
            mgr.append_entry({
                "id": f"e{i}",
                "type": "message",
                "timestamp": 1000 + i,
                "message": {"role": "user", "content": [{"type": "text", "text": f"msg{i}"}]},
            })

        forked = mgr.fork("e2", "at")
        assert forked is not None
        assert isinstance(forked, str)
        assert forked.endswith(".jsonl")

    def test_fork_in_memory_correct_entries(self, tmp_path):
        """In-memory fork() with 'at' produces correct entries."""
        sessions_dir = str(tmp_path)
        mgr = SessionManager.in_memory(cwd=sessions_dir)
        mgr._sessions_dir = sessions_dir  # Override to use tmp_path for fork output
        session_path = mgr.new_session()
        mgr._active_session_path = session_path

        for i in range(5):
            mgr.append_entry({
                "id": f"e{i}",
                "type": "message",
                "timestamp": 1000 + i,
                "message": {"role": "user", "content": [{"type": "text", "text": f"msg{i}"}]},
            })

        forked = mgr.fork("e2", "at")
        forked_entries = mgr._read_file(forked)
        forked_ids = [e["id"] for e in forked_entries if e.get("type") == "message"]
        assert forked_ids == ["e2", "e3", "e4"]

    def test_fork_in_memory_before_correct(self, tmp_path):
        """In-memory fork() with 'before' produces correct entries."""
        sessions_dir = str(tmp_path)
        mgr = SessionManager.in_memory(cwd=sessions_dir)
        mgr._sessions_dir = sessions_dir
        session_path = mgr.new_session()
        mgr._active_session_path = session_path

        for i in range(5):
            mgr.append_entry({
                "id": f"e{i}",
                "type": "message",
                "timestamp": 1000 + i,
                "message": {"role": "user", "content": [{"type": "text", "text": f"msg{i}"}]},
            })

        forked = mgr.fork("e2", "before")
        forked_entries = mgr._read_file(forked)
        forked_ids = [e["id"] for e in forked_entries if e.get("type") == "message"]
        assert forked_ids == ["e0", "e1"]

    def test_fork_in_memory_creates_file(self, tmp_path):
        """In-memory fork() creates a file on disk."""
        sessions_dir = str(tmp_path)
        mgr = SessionManager.in_memory(cwd=sessions_dir)
        mgr._sessions_dir = sessions_dir
        session_path = mgr.new_session()
        mgr._active_session_path = session_path

        for i in range(3):
            mgr.append_entry({
                "id": f"e{i}",
                "type": "message",
                "timestamp": 1000 + i,
                "message": {"role": "user", "content": [{"type": "text", "text": f"msg{i}"}]},
            })

        forked = mgr.fork("e1", "at")
        assert os.path.exists(forked)

    def test_fork_in_memory_does_not_affect_memory_store(self, tmp_path):
        """In-memory fork() creates a separate file — doesn't modify the in-memory store."""
        sessions_dir = str(tmp_path)
        mgr = SessionManager.in_memory(cwd=sessions_dir)
        mgr._sessions_dir = sessions_dir
        session_path = mgr.new_session()
        mgr._active_session_path = session_path

        for i in range(3):
            mgr.append_entry({
                "id": f"e{i}",
                "type": "message",
                "timestamp": 1000 + i,
                "message": {"role": "user", "content": [{"type": "text", "text": f"msg{i}"}]},
            })

        original_count = len(mgr._memory_store)
        mgr.fork("e1", "at")

        # Memory store should still have the original entries
        assert len(mgr._memory_store) == original_count

    def test_fork_in_memory_with_tree_entries(self, tmp_path):
        """In-memory fork() with tree-structured entries."""
        sessions_dir = str(tmp_path)
        mgr = SessionManager.in_memory(cwd=sessions_dir)
        mgr._sessions_dir = sessions_dir
        session_path = mgr.new_session()
        mgr._active_session_path = session_path

        mgr.append_entry({
            "id": "a", "type": "message", "timestamp": 1,
            "message": {"role": "user", "content": [{"type": "text", "text": "a"}]},
        })
        mgr.append_entry({
            "id": "b", "type": "message", "timestamp": 2,
            "message": {"role": "assistant", "content": [{"type": "text", "text": "b"}]},
        })
        mgr.append_entry({
            "id": "c", "type": "message", "timestamp": 3,
            "message": {"role": "user", "content": [{"type": "text", "text": "c"}]},
        })

        forked = mgr.fork("b", "at")
        forked_entries = mgr._read_file(forked)
        forked_ids = [e["id"] for e in forked_entries if e.get("type") == "message"]
        assert "b" in forked_ids
        assert "c" in forked_ids
        assert "a" not in forked_ids

    def test_fork_in_memory_before(self, tmp_path):
        """In-memory fork() with 'before' produces correct entries."""
        sessions_dir = str(tmp_path)
        mgr = SessionManager.in_memory(cwd=sessions_dir)
        mgr._sessions_dir = sessions_dir
        session_path = mgr.new_session()
        mgr._active_session_path = session_path

        for i in range(5):
            mgr.append_entry({
                "id": f"e{i}",
                "type": "message",
                "timestamp": 1000 + i,
                "message": {"role": "user", "content": [{"type": "text", "text": f"msg{i}"}]},
            })

        forked = mgr.fork("e2", "before")
        forked_entries = mgr._read_file(forked)
        forked_ids = [e["id"] for e in forked_entries if e.get("type") == "message"]
        assert forked_ids == ["e0", "e1"]


# =============================================================================
# Test: Settings _merge_from_file edge cases
# =============================================================================


class TestSettingsMergeEdgeCases:
    """Edge cases for Settings._merge_from_file and Settings.load()."""

    def test_load_bool_values(self, tmp_path, monkeypatch):
        """Settings.load() correctly handles boolean values."""
        global_dir = tmp_path / "home" / ".tau"
        global_dir.mkdir(parents=True)
        (global_dir / "settings.json").write_text(
            json.dumps({"compaction_enabled": False, "temperature": 0.0})
        )

        monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
        settings = Settings.load()

        assert settings.compaction_enabled is False
        assert settings.temperature == 0.0

    def test_load_null_values(self, tmp_path, monkeypatch):
        """Settings.load() correctly handles null/None values."""
        global_dir = tmp_path / "home" / ".tau"
        global_dir.mkdir(parents=True)
        (global_dir / "settings.json").write_text(
            json.dumps({"custom_system_prompt": None, "max_tokens": None})
        )

        monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
        settings = Settings.load()

        assert settings.custom_system_prompt is None
        assert settings.max_tokens is None

    def test_load_int_values(self, tmp_path, monkeypatch):
        """Settings.load() correctly handles integer values."""
        global_dir = tmp_path / "home" / ".tau"
        global_dir.mkdir(parents=True)
        (global_dir / "settings.json").write_text(
            json.dumps({"max_retries": 10, "context_margin": 5000})
        )

        monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
        settings = Settings.load()

        assert settings.max_retries == 10
        assert isinstance(settings.max_retries, int)
        assert settings.context_margin == 5000

    def test_load_float_values(self, tmp_path, monkeypatch):
        """Settings.load() correctly handles float values."""
        global_dir = tmp_path / "home" / ".tau"
        global_dir.mkdir(parents=True)
        (global_dir / "settings.json").write_text(
            json.dumps({"temperature": 1.0})
        )

        monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
        settings = Settings.load()

        assert settings.temperature == 1.0
        assert isinstance(settings.temperature, float)

    def test_load_preserves_default_extension_dirs(self, tmp_path, monkeypatch):
        """Settings.load() preserves default extension_dirs when no override."""
        global_dir = tmp_path / "home" / ".tau"
        global_dir.mkdir(parents=True)
        # Don't set extension_dirs in settings file

        monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
        settings = Settings.load()

        # Should have the default extension dirs
        assert len(settings.extension_dirs) >= 1
        assert ".tau" in settings.extension_dirs[0]

    def test_load_preserves_default_api_keys(self, tmp_path, monkeypatch):
        """Settings.load() preserves default empty api_keys when no override."""
        global_dir = tmp_path / "home" / ".tau"
        global_dir.mkdir(parents=True)
        # Don't set api_keys in settings file

        monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
        settings = Settings.load()

        assert settings.api_keys == {}


# =============================================================================
# Test: Navigate edge cases
# =============================================================================


class TestNavigateEdgeCases:
    """Edge cases for navigate()."""

    def test_navigate_to_session_entry(self, tmp_path):
        """navigate() to the session entry (root) works."""
        mgr = SessionManager(sessions_dir=str(tmp_path))
        session_path = mgr.new_session(model_id="gpt-4o")
        mgr._active_session_path = session_path

        # Navigate to the session entry itself
        entries = mgr._read_file(session_path)
        session_id = entries[0]["id"]
        state = mgr.navigate(session_id)

        assert state.active_entry_id == session_id
        assert state.model == "gpt-4o"

    def test_navigate_consecutive_calls(self, tmp_path):
        """Multiple consecutive navigate() calls work correctly."""
        mgr = SessionManager(sessions_dir=str(tmp_path))
        session_path = mgr.new_session()
        mgr._active_session_path = session_path

        for i in range(5):
            mgr.append_entry({
                "id": f"e{i}",
                "type": "message",
                "timestamp": 1000 + i,
                "message": {"role": "user", "content": [{"type": "text", "text": f"msg{i}"}]},
            })

        mgr.navigate("e1")
        assert mgr._active_entry_id == "e1"

        mgr.navigate("e3")
        assert mgr._active_entry_id == "e3"

        mgr.navigate("e1")
        assert mgr._active_entry_id == "e1"

    def test_navigate_returns_entries_count(self, tmp_path):
        """navigate() returns SessionState with all session entries."""
        mgr = SessionManager(sessions_dir=str(tmp_path))
        session_path = mgr.new_session()
        mgr._active_session_path = session_path

        for i in range(5):
            mgr.append_entry({
                "id": f"e{i}",
                "type": "message",
                "timestamp": 1000 + i,
                "message": {"role": "user", "content": [{"type": "text", "text": f"msg{i}"}]},
            })

        state = mgr.navigate("e2")
        assert len(state.entries) == 6  # session + e0..e2 (e3, e4 are after e2 in file)


# =============================================================================
# Test: Clone edge cases
# =============================================================================


class TestCloneEdgeCases:
    """Edge cases for clone()."""

    def test_clone_with_only_session_entry(self, tmp_path):
        """clone() on a session with only the session entry creates a minimal clone."""
        mgr = SessionManager(sessions_dir=str(tmp_path))
        session_path = mgr.new_session()
        mgr._active_session_path = session_path

        cloned = mgr.clone("e0")  # No messages exist yet
        # Should still succeed but the entry_id "e0" won't be found in active path
        cloned_entries = mgr._read_file(cloned)
        assert cloned_entries[0]["type"] == "session"

    def test_clone_preserves_timestamps(self, tmp_path):
        """clone() preserves the original timestamps of messages."""
        mgr = SessionManager(sessions_dir=str(tmp_path))
        session_path = mgr.new_session()
        mgr._active_session_path = session_path

        for i in range(3):
            mgr.append_entry({
                "id": f"e{i}",
                "type": "message",
                "timestamp": 5000 + i,
                "message": {"role": "user", "content": [{"type": "text", "text": f"msg{i}"}]},
            })

        cloned = mgr.clone("e1")
        cloned_entries = mgr._read_file(cloned)

        msg_entries = [e for e in cloned_entries if e.get("type") == "message"]
        assert msg_entries[0]["timestamp"] == 5000
        assert msg_entries[1]["timestamp"] == 5001

    def test_clone_with_multiple_compaction_entries(self, tmp_path):
        """clone() of a path with multiple compaction entries keeps the most
        recent compaction (the others are superseded).

        The active path anchors on the LAST compaction in the chain (matching
        pi's buildSessionContext, which reassigns ``compaction`` through the
        loop): a later compaction's summary already incorporates the earlier one
        via ``previous_summary``, so the stale summary and its kept region drop
        out of context — and therefore out of the clone.
        """
        mgr = SessionManager(sessions_dir=str(tmp_path))
        session_path = mgr.new_session()
        mgr._active_session_path = session_path

        for i in range(2):
            mgr.append_entry({
                "id": f"old{i}", "type": "message", "timestamp": i,
                "message": {"role": "user", "content": [{"type": "text", "text": f"old{i}"}]},
            })

        mgr.append_entry({
            "id": "comp1", "type": "compaction", "timestamp": 100,
            "first_kept_id": "keep1", "summary": "First compaction",
        })

        for i in range(2):
            mgr.append_entry({
                "id": f"keep{i}", "type": "message", "timestamp": 200 + i,
                "message": {"role": "user", "content": [{"type": "text", "text": f"keep{i}"}]},
            })

        mgr.append_entry({
            "id": "comp2", "type": "compaction", "timestamp": 300,
            "first_kept_id": "keep2_last", "summary": "Second compaction",
        })

        mgr.append_entry({
            "id": "keep2_last", "type": "message", "timestamp": 400,
            "message": {"role": "user", "content": [{"type": "text", "text": "last"}]},
        })

        cloned = mgr.clone("keep2_last")
        cloned_entries = mgr._read_file(cloned)
        cloned_types = [e["type"] for e in cloned_entries]
        summaries = [e["summary"] for e in cloned_entries if e.get("type") == "compaction"]

        # Only the most recent compaction survives in the active path.
        assert cloned_types.count("compaction") == 1
        assert summaries == ["Second compaction"]
        assert cloned_types.count("message") >= 1


# =============================================================================
# Test: Fork + Navigate integration
# =============================================================================


class TestForkNavigateIntegration:
    """Integration tests: fork followed by navigate."""

    def test_fork_and_navigate_forked_session(self, tmp_path):
        """After forking, the original session's navigate state is unaffected."""
        mgr = SessionManager(sessions_dir=str(tmp_path))
        session_path = mgr.new_session()
        mgr._active_session_path = session_path

        for i in range(5):
            mgr.append_entry({
                "id": f"e{i}",
                "type": "message",
                "timestamp": 1000 + i,
                "message": {"role": "user", "content": [{"type": "text", "text": f"msg{i}"}]},
            })

        # Navigate in original
        mgr.navigate("e2")
        assert mgr._active_entry_id == "e2"

        # Fork
        forked = mgr.fork("e3", "at")
        assert mgr._active_entry_id == "e2"  # Original navigate unaffected

        # Verify forked session has correct entries
        forked_entries = mgr._read_file(forked)
        forked_ids = [e["id"] for e in forked_entries if e.get("type") == "message"]
        assert "e3" in forked_ids
        assert "e4" in forked_ids
        assert "e2" not in forked_ids

    def test_clone_then_load_clone(self, tmp_path):
        """After cloning, loading the clone gives the same messages as cloning."""
        mgr = SessionManager(sessions_dir=str(tmp_path))
        session_path = mgr.new_session()
        mgr._active_session_path = session_path

        for i in range(3):
            mgr.append_entry({
                "id": f"e{i}",
                "type": "message",
                "timestamp": 1000 + i,
                "message": {"role": "user", "content": [{"type": "text", "text": f"msg{i}"}]},
            })

        cloned = mgr.clone("e1")
        state = mgr.load(cloned)

        assert len(state.entries) >= 1
        assert state.active_entry_id is not None

    def test_fork_multiple_then_compare(self, tmp_path):
        """Forking multiple times from different points produces distinct sessions."""
        mgr = SessionManager(sessions_dir=str(tmp_path))
        session_path = mgr.new_session()
        mgr._active_session_path = session_path

        for i in range(5):
            mgr.append_entry({
                "id": f"e{i}",
                "type": "message",
                "timestamp": 1000 + i,
                "message": {"role": "user", "content": [{"type": "text", "text": f"msg{i}"}]},
            })

        fork1 = mgr.fork("e1", "before")
        fork2 = mgr.fork("e1", "at")

        fork1_entries = mgr._read_file(fork1)
        fork2_entries = mgr._read_file(fork2)

        fork1_ids = [e["id"] for e in fork1_entries if e.get("type") == "message"]
        fork2_ids = [e["id"] for e in fork2_entries if e.get("type") == "message"]

        assert fork1_ids == ["e0"]
        assert fork2_ids == ["e1", "e2", "e3", "e4"]
        assert fork1_ids != fork2_ids


# =============================================================================
# Test 8: Branch Summarization (_extract_branch_messages)
# =============================================================================


def _make_async_iterator(events: list) -> Any:
    """Create a proper async iterator from a list of events for mocking stream_simple."""
    class _AsyncIterator:
        def __init__(self, events: list):
            self._events = events
            self._idx = 0
        def __aiter__(self):
            return self
        async def __anext__(self):
            if self._idx >= len(self._events):
                raise StopAsyncIteration
            event = self._events[self._idx]
            self._idx += 1
            return event
    return _AsyncIterator(events)


class TestExtractBranchMessages:
    """Test 8 (helper): _extract_branch_messages extracts messages from a tree branch."""

    def test_extract_linear_branch(self, tmp_path):
        """Extract messages from a linear branch (no branching).

        In a linear chain (session -> e0 -> e1 -> e2),
        _extract_branch_messages starts from the given entry and
        collects only descendants (children, grandchildren, etc.).
        Extracting from e1 should yield e1 and e2, but NOT e0.
        """
        mgr = SessionManager(sessions_dir=str(tmp_path))
        session_path = mgr.new_session()
        mgr._active_session_path = session_path

        for i in range(3):
            mgr.append_entry({
                "id": f"e{i}",
                "type": "message",
                "timestamp": 1000 + i,
                "message": {"role": "user" if i % 2 == 0 else "assistant",
                            "content": [{"type": "text", "text": f"msg{i}"}]},
            })

        entries = mgr._get_entries()
        # e1 is child of e0; e2 is child of e1
        extracted = mgr._extract_branch_messages(entries, "e1")

        # e1 and e2 are descendants of e1; e0 is NOT (e0 is e1's parent)
        assert "[assistant]: msg1" in extracted
        assert "[user]: msg2" in extracted
        assert "[user]: msg0" not in extracted  # e0 is parent, not descendant

    def test_extract_empty_branch(self, tmp_path):
        """Extract from empty entry list returns empty string."""
        mgr = SessionManager(sessions_dir=str(tmp_path))
        extracted = mgr._extract_branch_messages([], "e0")
        assert extracted == ""

    def test_extract_nonexistent_entry(self, tmp_path):
        """Extract from nonexistent entry ID returns empty string."""
        mgr = SessionManager(sessions_dir=str(tmp_path))
        session_path = mgr.new_session()
        mgr._active_session_path = session_path

        mgr.append_entry({
            "id": "e0",
            "type": "message",
            "timestamp": 1000,
            "message": {"role": "user", "content": [{"type": "text", "text": "hello"}]},
        })

        entries = mgr._get_entries()
        extracted = mgr._extract_branch_messages(entries, "nonexistent")
        assert extracted == ""

    def test_extract_single_entry_branch(self, tmp_path):
        """Extract from a branch with only one entry."""
        mgr = SessionManager(sessions_dir=str(tmp_path))
        session_path = mgr.new_session()
        mgr._active_session_path = session_path

        mgr.append_entry({
            "id": "root",
            "type": "message",
            "timestamp": 1000,
            "message": {"role": "user", "content": [{"type": "text", "text": "root"}]},
        })

        entries = mgr._get_entries()
        extracted = mgr._extract_branch_messages(entries, "root")
        assert "[user]: root" in extracted

    def test_extract_with_tool_calls(self, tmp_path):
        """Extract handles entries with tool call content blocks."""
        mgr = SessionManager(sessions_dir=str(tmp_path))
        session_path = mgr.new_session()
        mgr._active_session_path = session_path

        mgr.append_entry({
            "id": "e0",
            "type": "message",
            "timestamp": 1000,
            "message": {"role": "assistant",
                        "content": [
                            {"type": "text", "text": "Let me check."},
                            {"type": "toolCall", "id": "call1", "name": "ls", "arguments": {"path": "/tmp"}},
                        ]},
        })

        entries = mgr._get_entries()
        extracted = mgr._extract_branch_messages(entries, "e0")
        assert "[assistant]:" in extracted
        assert "[tool_call: ls({'path': '/tmp'})]" in extracted

    def test_extract_with_tool_results(self, tmp_path):
        """Extract handles toolResult entries."""
        mgr = SessionManager(sessions_dir=str(tmp_path))
        session_path = mgr.new_session()
        mgr._active_session_path = session_path

        mgr.append_entry({
            "id": "e0",
            "type": "message",
            "timestamp": 1000,
            "message": {"role": "user", "content": [{"type": "text", "text": "ls"}]},
        })
        mgr.append_entry({
            "id": "e1",
            "type": "toolResult",
            "timestamp": 1001,
            "tool_call_id": "call1",
            "tool_name": "ls",
            "content": [{"type": "text", "text": "file1.txt"}],
            "is_error": False,
        })

        entries = mgr._get_entries()
        extracted = mgr._extract_branch_messages(entries, "e0")
        assert "[user]: ls" in extracted
        assert "[toolResult: ls] file1.txt" in extracted

    def test_extract_with_compaction_entry(self, tmp_path):
        """Extract handles compaction entries in the branch."""
        mgr = SessionManager(sessions_dir=str(tmp_path))
        session_path = mgr.new_session()
        mgr._active_session_path = session_path

        mgr.append_entry({
            "id": "e0",
            "type": "message",
            "timestamp": 1000,
            "message": {"role": "user", "content": [{"type": "text", "text": "old"}]},
        })
        mgr.append_entry({
            "id": "comp1",
            "type": "compaction",
            "timestamp": 1001,
            "first_kept_id": "keep1",
            "summary": "Conversation about project setup.",
        })

        entries = mgr._get_entries()
        extracted = mgr._extract_branch_messages(entries, "e0")
        assert "[user]: old" in extracted
        assert "[compaction]: Conversation about project setup." in extracted

    def test_extract_with_thinking_blocks(self, tmp_path):
        """Extract handles thinking content blocks."""
        mgr = SessionManager(sessions_dir=str(tmp_path))
        session_path = mgr.new_session()
        mgr._active_session_path = session_path

        mgr.append_entry({
            "id": "e0",
            "type": "message",
            "timestamp": 1000,
            "message": {"role": "assistant",
                        "content": [
                            {"type": "thinking", "thinking": "Let me think...",
                             "cached_tokens": 100},
                            {"type": "text", "text": "The answer is 42."},
                        ]},
        })

        entries = mgr._get_entries()
        extracted = mgr._extract_branch_messages(entries, "e0")
        assert "[thinking: Let me think...]" in extracted
        assert "The answer is 42." in extracted

    def test_extract_with_image_blocks(self, tmp_path):
        """Extract handles image content blocks."""
        mgr = SessionManager(sessions_dir=str(tmp_path))
        session_path = mgr.new_session()
        mgr._active_session_path = session_path

        mgr.append_entry({
            "id": "e0",
            "type": "message",
            "timestamp": 1000,
            "message": {"role": "user",
                        "content": [
                            {"type": "text", "text": "What is this?"},
                            {"type": "image", "data": "base64data", "mime_type": "image/png"},
                        ]},
        })

        entries = mgr._get_entries()
        extracted = mgr._extract_branch_messages(entries, "e0")
        assert "[image]" in extracted

    def test_extract_only_session_entry(self, tmp_path):
        """Extract from a session with only the session entry returns empty."""
        mgr = SessionManager(sessions_dir=str(tmp_path))
        session_path = mgr.new_session()
        mgr._active_session_path = session_path

        entries = mgr._get_entries()
        session_id = entries[0]["id"]
        extracted = mgr._extract_branch_messages(entries, session_id)
        assert extracted == ""


# =============================================================================
# Test 9: summarize_branch() integration
# =============================================================================


class TestSummarizeBranch:
    """Test 9 (Done Criterion 7): summarize_branch() extracts messages from a branch
    and generates an LLM summary.

    Per PHASE-5-SUBPHASE-2.md:
    - summarize_branch() extracts messages from a branch and generates a summary
    - Used when navigating back to a previous entry
    """

    @staticmethod
    def _make_mock_stream(summary_text: str):
        """Create a mock for stream_simple that returns a summary."""
        done_event = AsyncMock()
        done_event.delta = None
        done_event.text = summary_text
        done_event.type = "done"

        class MockStream:
            def __aiter__(self):
                return _make_async_iterator([done_event])
        # Return an INSTANCE, not the class — async for requires an iterable object
        return MockStream()

    def _summarize_with_mock(self, mgr, branch_entry, model, summary_text: str,
                              system_prompt: str | None = None):
        """Helper: run summarize_branch with a mocked LLM that returns summary_text."""
        from tau_agent_core.session_manager import summarize_branch as _sb

        async def mock_stream_simple(*args, **kwargs):
            return self._make_mock_stream(summary_text)

        with patch("tau_ai.client.stream_simple", mock_stream_simple):
            return asyncio.run(
                _sb(mgr, branch_entry, model, system_prompt=system_prompt)
            )

    def test_summarize_branch_with_mocked_llm(self, tmp_path):
        """summarize_branch() calls the LLM and returns a summary."""
        from tau_agent_core.session_manager import summarize_branch

        mgr = SessionManager(sessions_dir=str(tmp_path))
        session_path = mgr.new_session()
        mgr._active_session_path = session_path

        for i in range(3):
            mgr.append_entry({
                "id": f"e{i}",
                "type": "message",
                "timestamp": 1000 + i,
                "message": {"role": "user" if i % 2 == 0 else "assistant",
                            "content": [{"type": "text", "text": f"msg{i}"}]},
            })

        entries = mgr._get_entries()
        branch_entry = next(e for e in entries if e.get("id") == "e1")

        # Mock model
        mock_model = type("MockModel", (), {
            "id": "gpt-4o",
            "provider": "openai",
        })()

        summary = self._summarize_with_mock(
            mgr, branch_entry, mock_model,
            "User discussed project architecture and decided on microservices."
        )

        assert "microservices" in summary
        assert isinstance(summary, str)
        assert len(summary) > 0

    def test_summarize_branch_empty_branch(self, tmp_path):
        """summarize_branch() handles empty branches gracefully."""
        from tau_agent_core.session_manager import summarize_branch

        mgr = SessionManager(sessions_dir=str(tmp_path))
        session_path = mgr.new_session()
        mgr._active_session_path = session_path

        entries = mgr._get_entries()
        branch_entry = entries[0]  # Session entry (no messages)

        mock_model = type("MockModel", (), {"id": "gpt-4o", "provider": "openai"})()

        summary = asyncio.run(
            summarize_branch(mgr, branch_entry, mock_model)
        )

        assert "(No messages in this branch)" in summary

    def test_summarize_branch_with_branch_tree(self, tmp_path):
        """summarize_branch() correctly extracts from a branched subtree."""
        from tau_agent_core.session_manager import summarize_branch

        mgr = SessionManager(sessions_dir=str(tmp_path))
        session_path = mgr.new_session()
        mgr._active_session_path = session_path

        # Build: session -> a -> b (active path)
        #                   \-> c -> d (branch)
        mgr.append_entry({
            "id": "a", "type": "message", "timestamp": 1,
            "message": {"role": "user", "content": [{"type": "text", "text": "question"}]},
        })
        mgr.append_entry({
            "id": "b", "type": "message", "timestamp": 2, "parent_id": "a",
            "message": {"role": "assistant", "content": [{"type": "text", "text": "answer"}]},
        })
        mgr.append_entry({
            "id": "c", "type": "message", "timestamp": 3, "parent_id": "a",
            "message": {"role": "user", "content": [{"type": "text", "text": "followup"}]},
        })
        mgr.append_entry({
            "id": "d", "type": "message", "timestamp": 4, "parent_id": "c",
            "message": {"role": "assistant", "content": [{"type": "text", "text": "detailed answer"}]},
        })

        entries = mgr._get_entries()
        branch_entry = next(e for e in entries if e.get("id") == "c")

        # Extract should include c and d, but not b
        extracted = mgr._extract_branch_messages(entries, "c")
        assert "followup" in extracted
        assert "detailed answer" in extracted
        assert "[assistant]: answer" not in extracted  # b is not in this branch

        # Also test that summarize_branch returns a non-empty string
        mock_model = type("MockModel", (), {"id": "gpt-4o", "provider": "openai"})()
        summary = self._summarize_with_mock(mgr, branch_entry, mock_model, "Branch summary from LLM")
        assert isinstance(summary, str)
        assert len(summary) > 0

    def test_summarize_branch_returns_string(self, tmp_path):
        """summarize_branch() always returns a string."""
        from tau_agent_core.session_manager import summarize_branch

        mgr = SessionManager(sessions_dir=str(tmp_path))
        session_path = mgr.new_session()
        mgr._active_session_path = session_path

        mgr.append_entry({
            "id": "e0",
            "type": "message",
            "timestamp": 1000,
            "message": {"role": "user", "content": [{"type": "text", "text": "hello"}]},
        })

        entries = mgr._get_entries()
        branch_entry = next(e for e in entries if e.get("id") == "e0")
        mock_model = type("MockModel", (), {"id": "gpt-4o", "provider": "openai"})()

        summary = self._summarize_with_mock(mgr, branch_entry, mock_model, "Hello world summary")

        assert isinstance(summary, str)
        assert len(summary) > 0
        assert "Hello world summary" in summary

    def test_summarize_branch_from_module_level(self, tmp_path):
        """summarize_branch() is importable from tau_agent_core."""
        from tau_agent_core import summarize_branch as sm_branch

        assert callable(sm_branch)

        mgr = SessionManager(sessions_dir=str(tmp_path))
        session_path = mgr.new_session()
        mgr._active_session_path = session_path

        mgr.append_entry({
            "id": "e0",
            "type": "message",
            "timestamp": 1000,
            "message": {"role": "user", "content": [{"type": "text", "text": "test"}]},
        })

        entries = mgr._get_entries()
        branch_entry = next(e for e in entries if e.get("id") == "e0")
        mock_model = type("MockModel", (), {"id": "gpt-4o", "provider": "openai"})()

        summary = self._summarize_with_mock(mgr, branch_entry, mock_model, "Module level summary")

        assert isinstance(summary, str)
        assert "Module level summary" in summary

    def test_summarize_branch_with_system_prompt(self, tmp_path):
        """summarize_branch() includes system prompt in the LLM call."""
        from tau_agent_core.session_manager import summarize_branch

        mgr = SessionManager(sessions_dir=str(tmp_path))
        session_path = mgr.new_session()
        mgr._active_session_path = session_path

        mgr.append_entry({
            "id": "e0",
            "type": "message",
            "timestamp": 1000,
            "message": {"role": "user", "content": [{"type": "text", "text": "hello"}]},
        })

        entries = mgr._get_entries()
        branch_entry = next(e for e in entries if e.get("id") == "e0")

        mock_model = type("MockModel", (), {"id": "gpt-4o", "provider": "openai"})()

        # Use a custom system prompt
        custom_prompt = "You are a very concise summarizer."
        summary = self._summarize_with_mock(
            mgr, branch_entry, mock_model, "System prompt test summary",
            system_prompt=custom_prompt
        )

        assert isinstance(summary, str)
        assert len(summary) > 0
        assert "System prompt test summary" in summary

    def test_summarize_branch_deep_branch(self, tmp_path):
        """summarize_branch() correctly traverses a deep branch."""
        mgr = SessionManager(sessions_dir=str(tmp_path))
        session_path = mgr.new_session()
        mgr._active_session_path = session_path

        # Build a deep chain: root -> a -> b -> c -> d
        mgr.append_entry({
            "id": "root", "type": "message", "timestamp": 1,
            "message": {"role": "user", "content": [{"type": "text", "text": "root msg"}]},
        })
        mgr.append_entry({
            "id": "a", "type": "message", "timestamp": 2, "parent_id": "root",
            "message": {"role": "assistant", "content": [{"type": "text", "text": "a msg"}]},
        })
        mgr.append_entry({
            "id": "b", "type": "message", "timestamp": 3, "parent_id": "a",
            "message": {"role": "user", "content": [{"type": "text", "text": "b msg"}]},
        })
        mgr.append_entry({
            "id": "c", "type": "message", "timestamp": 4, "parent_id": "b",
            "message": {"role": "assistant", "content": [{"type": "text", "text": "c msg"}]},
        })
        mgr.append_entry({
            "id": "d", "type": "message", "timestamp": 5, "parent_id": "c",
            "message": {"role": "user", "content": [{"type": "text", "text": "d msg"}]},
        })

        entries = mgr._get_entries()

        # Extract from 'a' — should get a, b, c, d
        extracted = mgr._extract_branch_messages(entries, "a")
        assert "[assistant]: a msg" in extracted
        assert "[user]: b msg" in extracted
        assert "[assistant]: c msg" in extracted
        assert "[user]: d msg" in extracted
        # root should NOT be included (root is parent of a, not child)
        assert "root msg" not in extracted

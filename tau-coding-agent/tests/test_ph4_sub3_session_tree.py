"""Phase 4 Subphase 3 — Session Tree Widget Tests.

Tests for SessionTreeWidget:
1. SessionTreeWidget loads sessions from the session manager
2. Tree node labeling (session, message, compaction)
3. Tree selection
4. Session tree refresh

Reference: PHASE-4-SUBPHASE-3.md — Testing Strategy
Reference: SUBPHASE-0.0.md — SessionManager interface
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from tau_coding_agent.widgets.session_tree import SessionTreeWidget, SessionInfo
from tau_agent_core import SessionManager, SessionInfo as SMSessionInfo


# ===========================================================================
# Test 1: SessionTreeWidget loads sessions
# ===========================================================================


class TestSessionTreeLoads:
    """Test that SessionTreeWidget correctly loads sessions."""

    def test_session_tree_loads_no_sessions(self):
        """SessionTreeWidget loads with zero sessions."""
        mgr = SessionManager.in_memory()
        tree = SessionTreeWidget(mgr, on_select=lambda s: None)
        assert len(tree.root_children) == 0

    def test_session_tree_loads_single_session(self):
        """SessionTreeWidget shows one root for one session."""
        mgr = SessionManager.in_memory()
        mgr.new_session()

        tree = SessionTreeWidget(mgr, on_select=lambda s: None)
        assert len(tree.root_children) == 1

    def test_session_tree_loads_multiple_sessions(self):
        """SessionTreeWidget shows all sessions as root nodes."""
        mgr = SessionManager.in_memory()
        mgr.new_session()
        mgr.new_session()
        mgr.new_session()

        tree = SessionTreeWidget(mgr, on_select=lambda s: None)
        assert len(tree.root_children) == 3

    def test_session_tree_root_is_session_path(self):
        """Root keys in SessionTreeWidget are session paths."""
        mgr = SessionManager.in_memory()
        path1 = mgr.new_session()
        path2 = mgr.new_session()

        tree = SessionTreeWidget(mgr, on_select=lambda s: None)
        assert path1 in tree.root_children
        assert path2 in tree.root_children

    def test_session_tree_labels_use_directory_name(self):
        """Root labels use directory name with folder emoji."""
        mgr = SessionManager.in_memory()
        mgr.new_session()

        tree = SessionTreeWidget(mgr, on_select=lambda s: None)
        labels = tree.root_labels
        assert len(labels) == 1
        assert labels[0].startswith("📁")
        # Should contain directory name
        cwd = mgr.cwd
        assert Path(cwd).name in labels[0]


# ===========================================================================
# Test 2: Tree node labeling
# ===========================================================================


class TestTreeNodeLabeling:
    """Test that tree nodes are correctly labeled."""

    def test_user_message_label_prefix(self):
        """User message entries get 📝 prefix."""
        mgr = SessionManager.in_memory()
        session_path = mgr.new_session()
        mgr._active_session_path = session_path
        mgr.append_entry({
            "id": str(uuid.uuid4().hex),
            "type": "message",
            "timestamp": 1,
            "message": {
                "role": "user",
                "content": [{"type": "text", "text": "hello world"}],
            },
        })

        tree = SessionTreeWidget(mgr, on_select=lambda s: None)
        entries = tree.get_children(tree.root_children[0])
        labels = [e["label"] for e in entries]
        assert any("📝" in l for l in labels)

    def test_user_message_label_contains_preview(self):
        """User message label contains the first 50 chars of content."""
        mgr = SessionManager.in_memory()
        session_path = mgr.new_session()
        mgr._active_session_path = session_path
        mgr.append_entry({
            "id": str(uuid.uuid4().hex),
            "type": "message",
            "timestamp": 1,
            "message": {
                "role": "user",
                "content": [{"type": "text", "text": "hello world"}],
            },
        })

        tree = SessionTreeWidget(mgr, on_select=lambda s: None)
        entries = tree.get_children(tree.root_children[0])
        labels = [e["label"] for e in entries]
        # "hello world" should appear in a label
        assert any("hello world" in l for l in labels)

    def test_assistant_message_label(self):
        """Assistant message entries get 💬 assistant label."""
        mgr = SessionManager.in_memory()
        session_path = mgr.new_session()
        mgr._active_session_path = session_path
        mgr.append_entry({
            "id": str(uuid.uuid4().hex),
            "type": "message",
            "timestamp": 1,
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": "I can help"}],
            },
        })

        tree = SessionTreeWidget(mgr, on_select=lambda s: None)
        entries = tree.get_children(tree.root_children[0])
        labels = [e["label"] for e in entries]
        assert any("💬 assistant" in l for l in labels)

    def test_compaction_label_shows_tokens(self):
        """Compaction entries show tokens saved in label."""
        mgr = SessionManager.in_memory()
        session_path = mgr.new_session()
        mgr._active_session_path = session_path
        mgr.append_entry({
            "id": str(uuid.uuid4().hex),
            "type": "compaction",
            "timestamp": 1,
            "tokens_saved": 150,
        })

        tree = SessionTreeWidget(mgr, on_select=lambda s: None)
        entries = tree.get_children(tree.root_children[0])
        labels = [e["label"] for e in entries]
        assert any("📦 Compaction (150 tokens saved)" in l for l in labels)

    def test_unknown_entry_type_gets_emoji_prefix(self):
        """Unknown entry types get ❓ prefix with type name."""
        mgr = SessionManager.in_memory()
        session_path = mgr.new_session()
        mgr._active_session_path = session_path
        mgr.append_entry({
            "id": str(uuid.uuid4().hex),
            "type": "custom",
            "timestamp": 1,
        })

        tree = SessionTreeWidget(mgr, on_select=lambda s: None)
        entries = tree.get_children(tree.root_children[0])
        labels = [e["label"] for e in entries]
        assert any("❓ custom" in l for l in labels)

    def test_session_entry_label(self):
        """Session sub-entries get 🗂 prefix with name."""
        mgr = SessionManager.in_memory()
        session_path = mgr.new_session()
        mgr._active_session_path = session_path
        mgr.append_entry({
            "id": str(uuid.uuid4().hex),
            "type": "session",
            "session_name": "My Session",
            "timestamp": 1,
        })

        tree = SessionTreeWidget(mgr, on_select=lambda s: None)
        entries = tree.get_children(tree.root_children[0])
        labels = [e["label"] for e in entries]
        assert any("🗂 My Session" in l for l in labels)


# ===========================================================================
# Test 3: Tree selection
# ===========================================================================


class TestTreeSelection:
    """Test session/entry selection via tree."""

    def test_selection_callback_invoked(self):
        """on_select callback is invoked when a session is selected."""
        mgr = SessionManager.in_memory()
        mgr.new_session()
        selected_items = []
        tree = SessionTreeWidget(mgr, on_select=lambda s: selected_items.append(s))

        tree.on_select(tree.root_children[0])
        assert len(selected_items) == 1

    def test_selected_item_is_session_info(self):
        """Selection returns a SessionInfo with correct data."""
        mgr = SessionManager.in_memory()
        mgr.new_session()
        selected_items = []
        tree = SessionTreeWidget(mgr, on_select=lambda s: selected_items.append(s))

        tree.on_select(tree.root_children[0])
        item = selected_items[0]
        assert isinstance(item, SessionInfo)
        assert item.session_path != ""

    def test_tree_get_selected_returns_session_info(self):
        """get_selected() returns the selected SessionInfo."""
        mgr = SessionManager.in_memory()
        mgr.new_session()
        selected_items = []
        tree = SessionTreeWidget(mgr, on_select=lambda s: selected_items.append(s))

        tree.on_select(tree.root_children[0])
        assert tree.get_selected() is not None
        assert isinstance(tree.get_selected(), SessionInfo)

    def test_session_info_from_dict(self):
        """SessionInfo.from_dict creates a proper SessionInfo."""
        d = {
            "session_path": "/tmp/test.jsonl",
            "cwd": "/tmp",
            "model": "gpt-4o",
            "model_name": "GPT-4o",
            "timestamp": 1700000000000,
        }
        si = SessionInfo.from_dict(d)
        assert si.session_path == "/tmp/test.jsonl"
        assert si.cwd == "/tmp"
        assert si.model == "gpt-4o"
        assert si.model_name == "GPT-4o"
        assert si.timestamp == 1700000000000

    def test_session_info_from_dict_defaults(self):
        """SessionInfo.from_dict handles missing fields."""
        d = {}
        si = SessionInfo.from_dict(d)
        assert si.session_path == ""
        assert si.cwd == ""
        assert si.model is None
        assert si.timestamp is None

    def test_session_info_from_sm_info(self):
        """SessionInfo.from_session_manager_info converts SM SessionInfo."""
        sm_info = SMSessionInfo(
            session_path="/tmp/session.jsonl",
            cwd="/tmp",
            model="claude-3.5-sonnet",
            model_name="Claude 3.5 Sonnet",
            created_at=1700000000000,
        )
        si = SessionInfo.from_session_manager_info(sm_info)
        assert si.session_path == "/tmp/session.jsonl"
        assert si.model == "claude-3.5-sonnet"
        assert si.model_name == "Claude 3.5 Sonnet"


# ===========================================================================
# Test 4: Session refresh
# ===========================================================================


class TestSessionRefresh:
    """Test tree refresh after new sessions created."""

    def test_refresh_after_new_session(self):
        """Tree shows new sessions after refresh()."""
        mgr = SessionManager.in_memory()
        mgr.new_session()
        tree = SessionTreeWidget(mgr, on_select=lambda s: None)
        assert len(tree.root_children) == 1

        mgr.new_session()
        tree.refresh()
        assert len(tree.root_children) == 2

    def test_refresh_clears_previous_state(self):
        """Refresh rebuilds the tree from scratch."""
        mgr = SessionManager.in_memory()
        path1 = mgr.new_session()
        tree = SessionTreeWidget(mgr, on_select=lambda s: None)
        assert path1 in tree.root_children

        # Delete the session file (simulate removal)
        mgr._active_session_path = None
        # Add a new session
        mgr.new_session()
        tree.refresh()
        # Only the new session should be in roots
        assert len(tree.root_children) == 2

    def test_refresh_with_entries(self):
        """Refresh correctly loads entries for each session."""
        mgr = SessionManager.in_memory()
        session_path = mgr.new_session()
        mgr._active_session_path = session_path
        mgr.append_entry({
            "id": str(uuid.uuid4().hex),
            "type": "message",
            "message": {"role": "user", "content": [{"type": "text", "text": "test"}]},
        })

        tree = SessionTreeWidget(mgr, on_select=lambda s: None)
        entries = tree.get_children(tree.root_children[0])
        assert len(entries) == 1
        assert entries[0]["type"] == "message"

    def test_refresh_preserves_select_callback(self):
        """Refresh does not replace the select callback."""
        mgr = SessionManager.in_memory()
        mgr.new_session()
        selected = []
        tree = SessionTreeWidget(mgr, on_select=lambda s: selected.append(s))

        tree.refresh()
        tree.on_select(tree.root_children[0])
        assert len(selected) == 1

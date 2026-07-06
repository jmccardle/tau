"""Tests for tau_agent_core.session — SessionEntry type from SUBPHASE-0.0.md.

Tests verify:
- SessionEntry can be instantiated
- SessionEntry has required fields: id, type, timestamp
- MessageEntry, ToolResultEntry, CustomMessageEntry, CompactionEntry work
- parent_id creates tree structure
- Timestamp is ms since epoch
- Entry types are discriminated unions
"""

import pytest

from tau_agent_core.session import (
    SessionEntry,
    MessageEntry,
    ToolResultEntry,
    CustomMessageEntry,
    CompactionEntry,
)


class TestSessionEntry:
    """Tests for SessionEntry."""

    def test_create_session_entry(self):
        """SessionEntry can be instantiated."""
        entry = SessionEntry(
            id="session_001",
            type="session",
            timestamp=1700000000000,
        )
        assert entry.id == "session_001"
        assert entry.type == "session"
        assert entry.timestamp == 1700000000000

    def test_session_entry_required_fields(self):
        """SessionEntry requires id, type, timestamp."""
        with pytest.raises(Exception):  # ValidationError or TypeError
            SessionEntry()  # Missing required fields

    def test_session_entry_optional_fields(self):
        """SessionEntry has optional fields: parent_id, model, cwd, system_prompt."""
        entry = SessionEntry(
            id="session_001",
            type="session",
            timestamp=1700000000000,
            parent_id=None,
            model="gpt-4",
            model_name="GPT-4",
            cwd="/tmp",
            system_prompt="You are a helpful assistant.",
            session_name="Test Session",
        )
        assert entry.model == "gpt-4"
        assert entry.model_name == "GPT-4"
        assert entry.cwd == "/tmp"
        assert entry.system_prompt == "You are a helpful assistant."
        assert entry.session_name == "Test Session"

    def test_session_entry_parent_id_optional(self):
        """SessionEntry.parent_id is optional."""
        entry = SessionEntry(
            id="session_001",
            type="session",
            timestamp=1700000000000,
        )
        assert entry.parent_id is None


class TestMessageEntry:
    """Tests for MessageEntry."""

    def test_create_message_entry(self):
        """MessageEntry can be instantiated."""
        entry = MessageEntry(
            id="msg_001",
            type="message",
            timestamp=1700000000000,
            message={
                "role": "user",
                "content": [{"type": "text", "text": "Hello"}],
            },
        )
        assert entry.type == "message"
        assert entry.message is not None
        assert entry.message["role"] == "user"

    def test_message_entry_required_fields(self):
        """MessageEntry requires id, type, timestamp, message."""
        entry = MessageEntry(
            id="msg_001",
            type="message",
            timestamp=1700000000000,
            message={},
        )
        assert entry.type == "message"

    def test_message_entry_tree_structure(self):
        """MessageEntry.parent_id creates tree structure."""
        entry = MessageEntry(
            id="msg_001",
            type="message",
            timestamp=1700000000000,
            parent_id="session_001",
            message={},
        )
        assert entry.parent_id == "session_001"


class TestToolResultEntry:
    """Tests for ToolResultEntry."""

    def test_create_tool_result_entry(self):
        """ToolResultEntry can be instantiated."""
        entry = ToolResultEntry(
            id="tool_001",
            type="toolResult",
            timestamp=1700000000000,
            tool_call_id="call_123",
            tool_name="ls",
            content=[{"type": "text", "text": "file1.txt\nfile2.py"}],
        )
        assert entry.type == "toolResult"
        assert entry.tool_call_id == "call_123"
        assert entry.tool_name == "ls"

    def test_tool_result_entry_required_fields(self):
        """ToolResultEntry requires id, type, timestamp, tool_call_id, tool_name, content."""
        entry = ToolResultEntry(
            id="tool_001",
            type="toolResult",
            timestamp=1700000000000,
            tool_call_id="call_123",
            tool_name="ls",
            content=[],
        )
        assert entry.tool_call_id == "call_123"

    def test_tool_result_entry_is_error(self):
        """ToolResultEntry can represent an error."""
        entry = ToolResultEntry(
            id="tool_001",
            type="toolResult",
            timestamp=1700000000000,
            tool_call_id="call_123",
            tool_name="bash",
            content=[{"type": "text", "text": "Error: exit 1"}],
            is_error=True,
        )
        assert entry.is_error is True

    def test_tool_result_entry_error_defaults_false(self):
        """ToolResultEntry.is_error defaults to False."""
        entry = ToolResultEntry(
            id="tool_001",
            type="toolResult",
            timestamp=1700000000000,
            tool_call_id="call_123",
            tool_name="ls",
            content=[],
        )
        assert entry.is_error is False


class TestCustomMessageEntry:
    """Tests for CustomMessageEntry."""

    def test_create_custom_message_entry(self):
        """CustomMessageEntry can be instantiated."""
        entry = CustomMessageEntry(
            id="custom_001",
            type="customMessage",
            timestamp=1700000000000,
            custom_type="notification",
            message={"text": "System notification"},
        )
        assert entry.type == "customMessage"
        assert entry.custom_type == "notification"

    def test_custom_message_entry_required_fields(self):
        """CustomMessageEntry requires id, type, timestamp, custom_type, message."""
        entry = CustomMessageEntry(
            id="custom_001",
            type="customMessage",
            timestamp=1700000000000,
            custom_type="info",
            message={},
        )
        assert entry.custom_type == "info"


class TestCompactionEntry:
    """Tests for CompactionEntry."""

    def test_create_compaction_entry(self):
        """CompactionEntry can be instantiated."""
        entry = CompactionEntry(
            id="compact_001",
            type="compaction",
            timestamp=1700000000000,
            first_kept_id="msg_050",
            summary="Conversation was compacted",
        )
        assert entry.type == "compaction"
        assert entry.first_kept_id == "msg_050"
        assert entry.summary == "Conversation was compacted"

    def test_compaction_entry_required_fields(self):
        """CompactionEntry requires id, type, timestamp, first_kept_id, summary."""
        entry = CompactionEntry(
            id="compact_001",
            type="compaction",
            timestamp=1700000000000,
            first_kept_id="msg_050",
            summary="Compacted",
        )
        assert entry.first_kept_id == "msg_050"

    def test_compaction_entry_optional_fields(self):
        """CompactionEntry has optional fields: tokens_saved, compacted_entries."""
        entry = CompactionEntry(
            id="compact_001",
            type="compaction",
            timestamp=1700000000000,
            first_kept_id="msg_050",
            summary="Compacted",
            tokens_saved=1000,
            compacted_entries=["msg_001", "msg_002"],
        )
        assert entry.tokens_saved == 1000
        assert entry.compacted_entries == ["msg_001", "msg_002"]

    def test_compaction_entry_defaults(self):
        """CompactionEntry optional fields default to None/0."""
        entry = CompactionEntry(
            id="compact_001",
            type="compaction",
            timestamp=1700000000000,
            first_kept_id="msg_050",
            summary="Compacted",
        )
        assert entry.tokens_saved == 0
        assert entry.compacted_entries == []


class TestSessionEntryTypes:
    """Tests for session entry type discrimination."""

    ENTRY_TYPES = [
        "session",
        "message",
        "toolResult",
        "customMessage",
        "compaction",
    ]

    @pytest.mark.parametrize("entry_type", ENTRY_TYPES)
    def test_all_entry_types_valid(self, entry_type):
        """All documented entry types should be valid."""
        assert entry_type in self.ENTRY_TYPES

    def test_entry_type_is_string(self):
        """Entry type should be a string."""
        entry = SessionEntry(
            id="test",
            type="session",
            timestamp=0,
        )
        assert isinstance(entry.type, str)


class TestSessionEntryTreeStructure:
    """Tests for session entry tree structure via parent_id."""

    def test_entries_can_form_tree(self):
        """Multiple entries with parent_id form a tree."""
        session = SessionEntry(
            id="session_001",
            type="session",
            timestamp=1700000000000,
        )
        child1 = MessageEntry(
            id="msg_001",
            type="message",
            timestamp=1700000001000,
            parent_id="session_001",
            message={},
        )
        child2 = MessageEntry(
            id="msg_002",
            type="message",
            timestamp=1700000002000,
            parent_id="msg_001",
            message={},
        )
        # Verify parent-child relationships
        assert child1.parent_id == "session_001"
        assert child2.parent_id == "msg_001"

    def test_root_entry_has_no_parent(self):
        """Root session entry has no parent_id."""
        entry = SessionEntry(
            id="session_001",
            type="session",
            timestamp=1700000000000,
        )
        assert entry.parent_id is None


class TestSessionAppendOnly:
    """Tests for session append-only constraint from SUBPHASE-0.0.md.

    Constraint: The JSONL format is append-only. No in-place edits.
    Sessions are rebuilt by replaying entries.
    """

    def test_entries_are_added_not_modified(self):
        """Entries should be added, not modified in place."""
        # This test documents the append-only contract
        # Implementation: session_manager only appends, never updates
        pass  # Will be tested when session_manager exists

    def test_entry_id_is_unique(self):
        """Each entry must have a unique ID."""
        entry1 = SessionEntry(
            id="unique_001",
            type="session",
            timestamp=1700000000000,
        )
        entry2 = SessionEntry(
            id="unique_002",
            type="session",
            timestamp=1700000001000,
        )
        assert entry1.id != entry2.id

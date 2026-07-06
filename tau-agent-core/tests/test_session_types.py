"""Tests for tau_agent_core.session — SessionState and SessionInfo types.

Tests verify:
- SessionState tracks session lifecycle state
- SessionInfo holds lightweight session metadata
- All fields are properly typed with defaults
- Both types serialize correctly (Pydantic models)

Reference: SUBPHASE-0.0.md, "6. Session Entry JSON Schema" section
Reference: PHASE-2-SUBPHASE-0.md, Scope (session.py should have SessionState, SessionInfo)
"""

import pytest

from pydantic import ValidationError

from tau_agent_core.session import SessionInfo, SessionState


class TestSessionState:
    """Tests for SessionState."""

    def test_create_session_state(self):
        """SessionState can be instantiated with required fields."""
        state = SessionState(
            session_id="session_001",
            created_at=1700000000000,
            updated_at=1700000001000,
        )
        assert state.session_id == "session_001"
        assert state.status == "idle"
        assert state.message_count == 0
        assert state.turn_count == 0
        assert state.is_compacted is False

    def test_session_state_defaults(self):
        """SessionState has sensible defaults."""
        state = SessionState(session_id="session_001")
        assert state.status == "idle"
        assert state.message_count == 0
        assert state.turn_count == 0
        assert state.is_compacted is False

    def test_session_state_running_status(self):
        """SessionState tracks 'running' status."""
        state = SessionState(
            session_id="session_001",
            status="running",
            message_count=5,
            turn_count=2,
        )
        assert state.status == "running"

    def test_session_state_aborting_status(self):
        """SessionState tracks 'aborting' status."""
        state = SessionState(
            session_id="session_001",
            status="aborting",
        )
        assert state.status == "aborting"

    def test_session_state_error_status(self):
        """SessionState tracks 'error' status."""
        state = SessionState(
            session_id="session_001",
            status="error",
        )
        assert state.status == "error"

    def test_session_state_compacted(self):
        """SessionState tracks whether session has been compacted."""
        state = SessionState(
            session_id="session_001",
            is_compacted=True,
        )
        assert state.is_compacted is True

    def test_session_state_all_fields(self):
        """SessionState accepts all fields."""
        state = SessionState(
            session_id="session_001",
            status="running",
            message_count=10,
            turn_count=3,
            is_compacted=False,
            created_at=1700000000000,
            updated_at=1700000005000,
        )
        assert state.session_id == "session_001"
        assert state.status == "running"
        assert state.message_count == 10
        assert state.turn_count == 3
        assert state.is_compacted is False
        assert state.created_at == 1700000000000
        assert state.updated_at == 1700000005000

    def test_session_state_serialization(self):
        """SessionState serializes to dict correctly."""
        state = SessionState(
            session_id="session_001",
            status="idle",
            message_count=5,
        )
        data = state.model_dump()
        assert data["session_id"] == "session_001"
        assert data["status"] == "idle"

    def test_session_state_json(self):
        """SessionState serializes to JSON string."""
        state = SessionState(
            session_id="session_001",
            status="running",
            message_count=10,
            turn_count=3,
        )
        json_str = state.model_dump_json()
        assert '"session_id":"session_001"' in json_str
        assert '"status":"running"' in json_str
        assert '"message_count":10' in json_str

    def test_session_state_status_values(self):
        """SessionState only accepts valid status values."""
        valid_statuses = ["idle", "running", "aborting", "error"]
        for status in valid_statuses:
            state = SessionState(session_id="s", status=status)
            assert state.status == status

    def test_session_state_rejects_invalid_status(self):
        """SessionState rejects invalid status values."""
        with pytest.raises(ValidationError):
            SessionState(session_id="s", status="invalid_status")


class TestSessionInfo:
    """Tests for SessionInfo."""

    def test_create_session_info(self):
        """SessionInfo can be instantiated with required fields."""
        info = SessionInfo(
            id="session_001",
            created_at=1700000000000,
            updated_at=1700000001000,
        )
        assert info.id == "session_001"
        assert info.name is None
        assert info.message_count == 0
        assert info.turn_count == 0
        assert info.status == "idle"
        assert info.model is None
        assert info.tool_count == 0

    def test_session_info_defaults(self):
        """SessionInfo has sensible defaults."""
        info = SessionInfo(id="session_001")
        assert info.name is None
        assert info.created_at == 0
        assert info.updated_at == 0
        assert info.message_count == 0
        assert info.turn_count == 0
        assert info.status == "idle"
        assert info.model is None
        assert info.tool_count == 0

    def test_session_info_full(self):
        """SessionInfo accepts all fields."""
        info = SessionInfo(
            id="session_001",
            name="My Session",
            created_at=1700000000000,
            updated_at=1700000001000,
            message_count=15,
            turn_count=5,
            status="idle",
            model="gpt-4o",
            tool_count=8,
        )
        assert info.id == "session_001"
        assert info.name == "My Session"
        assert info.message_count == 15
        assert info.turn_count == 5
        assert info.status == "idle"
        assert info.model == "gpt-4o"
        assert info.tool_count == 8

    def test_session_info_serialization(self):
        """SessionInfo serializes to dict correctly."""
        info = SessionInfo(
            id="session_001",
            name="Test",
            message_count=10,
        )
        data = info.model_dump()
        assert data["id"] == "session_001"
        assert data["name"] == "Test"
        assert data["message_count"] == 10

    def test_session_info_json(self):
        """SessionInfo serializes to JSON string."""
        info = SessionInfo(id="session_001", name="Test", model="gpt-4o")
        json_str = info.model_dump_json()
        assert '"id":"session_001"' in json_str
        assert '"name":"Test"' in json_str
        assert '"model":"gpt-4o"' in json_str

    def test_session_info_status_values(self):
        """SessionInfo only accepts valid status values."""
        valid_statuses = ["idle", "running", "aborting", "error"]
        for status in valid_statuses:
            info = SessionInfo(id="s", status=status)
            assert info.status == status

    def test_session_info_rejects_invalid_status(self):
        """SessionInfo rejects invalid status values."""
        with pytest.raises(ValidationError):
            SessionInfo(id="s", status="invalid_status")

    def test_session_info_lightweight(self):
        """SessionInfo is lightweight — no full message content."""
        info = SessionInfo(
            id="session_001",
            message_count=42,
            turn_count=10,
        )
        assert info.message_count == 42
        assert info.turn_count == 10
        # SessionInfo should NOT contain actual messages
        assert not hasattr(info, "messages") or info.__pydantic_fields_set__ == {"id"}


class TestSessionTypesImport:
    """Tests for module-level imports.

    Reference: PHASE-2-SUBPHASE-0.md, Testing section item 1.
    > from tau_agent_core.session import SessionEntry, SessionState, SessionInfo
    """

    def test_import_session_state(self):
        """SessionState imports from session module."""
        from tau_agent_core.session import SessionState
        assert SessionState is not None

    def test_import_session_info(self):
        """SessionInfo imports from session module."""
        from tau_agent_core.session import SessionInfo
        assert SessionInfo is not None

    def test_import_from_package_root(self):
        """All session types import from tau_agent_core package root."""
        from tau_agent_core import (
            SessionEntry,
            SessionState,
            SessionInfo,
        )
        assert SessionEntry is not None
        assert SessionState is not None
        assert SessionInfo is not None


class TestSessionStateVsSessionInfo:
    """Tests distinguishing SessionState from SessionInfo."""

    def test_session_state_has_is_compacted(self):
        """SessionState tracks compaction status, SessionInfo does not."""
        state = SessionState(session_id="s", is_compacted=True)
        assert state.is_compacted is True

        info = SessionInfo(id="s")
        assert not hasattr(info, "is_compacted")

    def test_session_info_has_name(self):
        """SessionInfo tracks name, SessionState does not."""
        info = SessionInfo(id="s", name="My Session")
        assert info.name == "My Session"

        state = SessionState(session_id="s")
        assert not hasattr(state, "name")

    def test_session_info_has_tool_count(self):
        """SessionInfo tracks tool count, SessionState does not."""
        info = SessionInfo(id="s", tool_count=5)
        assert info.tool_count == 5

        state = SessionState(session_id="s")
        assert not hasattr(state, "tool_count")

    def test_both_track_message_count(self):
        """Both SessionState and SessionInfo track message_count."""
        state = SessionState(session_id="s", message_count=10)
        info = SessionInfo(id="s", message_count=10)
        assert state.message_count == info.message_count == 10

    def test_both_track_turn_count(self):
        """Both SessionState and SessionInfo track turn_count."""
        state = SessionState(session_id="s", turn_count=3)
        info = SessionInfo(id="s", turn_count=3)
        assert state.turn_count == info.turn_count == 3

    def test_both_track_status(self):
        """Both SessionState and SessionInfo track status."""
        state = SessionState(session_id="s", status="running")
        info = SessionInfo(id="s", status="running")
        assert state.status == info.status == "running"

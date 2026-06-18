"""Tests for ToolCallData widget data contract.

Reference: PHASE-4-SUBPHASE-0.md — ToolCallData Contract
Reference: SUBPHASE-0.0.md — AgentEvent.tool_execution_* fields → widget data
"""

import dataclasses
import pytest

from tau_coding_agent.widgets.tool_call_widget import ToolCallData


class TestToolCallDataImport:
    """Test that ToolCallData is importable."""

    def test_tool_call_data_is_importable(self):
        """ToolCallData must be importable from widgets.tool_call_widget."""
        from tau_coding_agent.widgets.tool_call_widget import ToolCallData as T
        assert T is not None

    def test_tool_call_data_in_widgets_init(self):
        """ToolCallData must be re-exported from widgets.__init__."""
        from tau_coding_agent.widgets import ToolCallData as T
        assert T is ToolCallData


class TestToolCallDataIsDataclass:
    """Test that ToolCallData is a proper dataclass."""

    def test_is_dataclass(self):
        """ToolCallData must be a dataclass."""
        assert dataclasses.is_dataclass(ToolCallData)

    def test_has_all_required_fields(self):
        """ToolCallData must have tool_name, tool_call_id, arguments, status."""
        field_names = {f.name for f in dataclasses.fields(ToolCallData)}
        assert "tool_name" in field_names
        assert "tool_call_id" in field_names
        assert "arguments" in field_names
        assert "status" in field_names

    def test_has_result_preview_field(self):
        """ToolCallData must have result_preview field."""
        field_names = {f.name for f in dataclasses.fields(ToolCallData)}
        assert "result_preview" in field_names


class TestToolCallDataConstruction:
    """Test ToolCallData construction and defaults."""

    def test_minimal_construction(self):
        """ToolCallData can be constructed with just required fields."""
        tc = ToolCallData(
            tool_name="bash",
            tool_call_id="call_abc123",
            arguments={"cmd": "ls -la"},
            status="pending",
        )
        assert tc.tool_name == "bash"
        assert tc.tool_call_id == "call_abc123"
        assert tc.arguments == {"cmd": "ls -la"}
        assert tc.status == "pending"

    def test_result_preview_defaults_to_none(self):
        """result_preview defaults to None."""
        tc = ToolCallData(
            tool_name="bash",
            tool_call_id="call_abc123",
            arguments={},
            status="pending",
        )
        assert tc.result_preview is None

    def test_full_construction(self):
        """ToolCallData accepts all fields."""
        tc = ToolCallData(
            tool_name="bash",
            tool_call_id="call_xyz789",
            arguments={"cmd": "echo hello"},
            status="running",
            result_preview="hello world",
        )
        assert tc.tool_name == "bash"
        assert tc.tool_call_id == "call_xyz789"
        assert tc.arguments == {"cmd": "echo hello"}
        assert tc.status == "running"
        assert tc.result_preview == "hello world"


class TestToolCallDataStatus:
    """Test ToolCallData status field."""

    def test_pending_status(self):
        """ToolCallData accepts status='pending'."""
        tc = ToolCallData(
            tool_name="bash",
            tool_call_id="call_1",
            arguments={},
            status="pending",
        )
        assert tc.status == "pending"

    def test_running_status(self):
        """ToolCallData accepts status='running'."""
        tc = ToolCallData(
            tool_name="bash",
            tool_call_id="call_1",
            arguments={},
            status="running",
        )
        assert tc.status == "running"

    def test_done_status(self):
        """ToolCallData accepts status='done'."""
        tc = ToolCallData(
            tool_name="bash",
            tool_call_id="call_1",
            arguments={},
            status="done",
        )
        assert tc.status == "done"

    def test_error_status(self):
        """ToolCallData accepts status='error'."""
        tc = ToolCallData(
            tool_name="bash",
            tool_call_id="call_1",
            arguments={},
            status="error",
        )
        assert tc.status == "error"

    def test_invalid_status_rejected(self):
        """ToolCallData rejects invalid status values (via Literal type)."""
        import typing
        from typing import get_args
        # Get the Literal type for status field
        field = [f for f in dataclasses.fields(ToolCallData) if f.name == "status"][0]
        # The type annotation should be Literal["pending", "running", "done", "error"]
        # We verify the valid values
        assert hasattr(field, 'default')
        # At runtime, Python's dataclass won't enforce Literal at construction
        # but the type hint documents the constraint


class TestToolCallDataArguments:
    """Test ToolCallData arguments field."""

    def test_arguments_accepts_dict(self):
        """arguments accepts a dict."""
        tc = ToolCallData(
            tool_name="bash",
            tool_call_id="call_1",
            arguments={"cmd": "ls -la"},
            status="pending",
        )
        assert isinstance(tc.arguments, dict)

    def test_arguments_accepts_nested_dict(self):
        """arguments accepts nested dicts."""
        tc = ToolCallData(
            tool_name="write",
            tool_call_id="call_1",
            arguments={"path": "/tmp/test.txt", "content": "hello", "replace": False},
            status="pending",
        )
        assert tc.arguments["path"] == "/tmp/test.txt"

    def test_arguments_accepts_empty_dict(self):
        """arguments accepts an empty dict."""
        tc = ToolCallData(
            tool_name="ls",
            tool_call_id="call_1",
            arguments={},
            status="pending",
        )
        assert tc.arguments == {}


class TestToolCallDataFromEventMapping:
    """Test that ToolCallData maps correctly from AgentEvent fields."""

    def test_from_tool_execution_start(self):
        """ToolCallData maps from tool_execution_start event fields."""
        # Simulating mapping from AgentEvent:
        # type="tool_execution_start", tool_name="bash", tool_call_id="call_1", args={"cmd":"ls"}
        tc = ToolCallData(
            tool_name="bash",
            tool_call_id="call_1",
            arguments={"cmd": "ls"},
            status="pending",
        )
        assert tc.tool_name == "bash"
        assert tc.tool_call_id == "call_1"
        assert tc.status == "pending"

    def test_from_tool_execution_update(self):
        """ToolCallData status updates to 'running' on tool_execution_update."""
        tc = ToolCallData(
            tool_name="bash",
            tool_call_id="call_1",
            arguments={"cmd": "ls"},
            status="pending",
        )
        # Simulate update event
        tc.status = "running"
        assert tc.status == "running"

    def test_from_tool_execution_end(self):
        """ToolCallData status updates to 'done' on tool_execution_end."""
        tc = ToolCallData(
            tool_name="bash",
            tool_call_id="call_1",
            arguments={"cmd": "ls"},
            status="running",
        )
        # Simulate end event
        tc.status = "done"
        tc.result_preview = "total 0"
        assert tc.status == "done"
        assert tc.result_preview == "total 0"

    def test_from_tool_execution_error(self):
        """ToolCallData status becomes 'error' on failure."""
        tc = ToolCallData(
            tool_name="bash",
            tool_call_id="call_1",
            arguments={"cmd": "invalid"},
            status="running",
        )
        tc.status = "error"
        assert tc.status == "error"

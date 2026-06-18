"""Tests for ToolResultData widget data contract.

Reference: PHASE-4-SUBPHASE-0.md — ToolResultData Contract
Reference: SUBPHASE-0.0.md — AgentEvent.tool_execution_end fields → widget data
"""

import dataclasses
import pytest

from tau_coding_agent.widgets.tool_result_widget import ToolResultData


class TestToolResultDataImport:
    """Test that ToolResultData is importable."""

    def test_tool_result_data_is_importable(self):
        """ToolResultData must be importable from widgets.tool_result_widget."""
        from tau_coding_agent.widgets.tool_result_widget import ToolResultData as T
        assert T is not None

    def test_tool_result_data_in_widgets_init(self):
        """ToolResultData must be re-exported from widgets.__init__."""
        from tau_coding_agent.widgets import ToolResultData as T
        assert T is ToolResultData


class TestToolResultDataIsDataclass:
    """Test that ToolResultData is a proper dataclass."""

    def test_is_dataclass(self):
        """ToolResultData must be a dataclass."""
        assert dataclasses.is_dataclass(ToolResultData)

    def test_has_all_required_fields(self):
        """ToolResultData must have tool_name, tool_call_id, result."""
        field_names = {f.name for f in dataclasses.fields(ToolResultData)}
        assert "tool_name" in field_names
        assert "tool_call_id" in field_names
        assert "result" in field_names

    def test_has_is_error_field(self):
        """ToolResultData must have is_error field."""
        field_names = {f.name for f in dataclasses.fields(ToolResultData)}
        assert "is_error" in field_names


class TestToolResultDataConstruction:
    """Test ToolResultData construction and defaults."""

    def test_minimal_construction(self):
        """ToolResultData can be constructed with required fields."""
        tr = ToolResultData(
            tool_name="bash",
            tool_call_id="call_abc123",
            result="file1.txt\nfile2.txt",
        )
        assert tr.tool_name == "bash"
        assert tr.tool_call_id == "call_abc123"
        assert tr.result == "file1.txt\nfile2.txt"

    def test_is_error_defaults_to_false(self):
        """is_error defaults to False."""
        tr = ToolResultData(
            tool_name="bash",
            tool_call_id="call_abc123",
            result="output",
        )
        assert tr.is_error is False

    def test_full_construction(self):
        """ToolResultData accepts all fields."""
        tr = ToolResultData(
            tool_name="write",
            tool_call_id="call_xyz789",
            result={"path": "/tmp/test.txt", "bytes_written": 1024},
            is_error=True,
        )
        assert tr.tool_name == "write"
        assert tr.tool_call_id == "call_xyz789"
        assert tr.is_error is True

    def test_result_accepts_string(self):
        """result accepts a string."""
        tr = ToolResultData(
            tool_name="bash",
            tool_call_id="call_1",
            result="command output",
        )
        assert tr.result == "command output"

    def test_result_accepts_dict(self):
        """result accepts a dict (for structured results)."""
        tr = ToolResultData(
            tool_name="read",
            tool_call_id="call_1",
            result={"content": "file content", "lines": 42},
        )
        assert isinstance(tr.result, dict)

    def test_result_accepts_any_type(self):
        """result accepts any type (arbitrary tool output)."""
        tr = ToolResultData(
            tool_name="ls",
            tool_call_id="call_1",
            result=["file1.txt", "file2.txt"],
        )
        assert isinstance(tr.result, list)

    def test_result_accepts_none(self):
        """result accepts None."""
        tr = ToolResultData(
            tool_name="touch",
            tool_call_id="call_1",
            result=None,
        )
        assert tr.result is None


class TestToolResultDataIsError:
    """Test ToolResultData is_error field."""

    def test_error_result(self):
        """is_error is True for error results."""
        tr = ToolResultData(
            tool_name="bash",
            tool_call_id="call_1",
            result="Error: command not found",
            is_error=True,
        )
        assert tr.is_error is True

    def test_success_result(self):
        """is_error is False for success results."""
        tr = ToolResultData(
            tool_name="ls",
            tool_call_id="call_1",
            result="file1.txt",
            is_error=False,
        )
        assert tr.is_error is False


class TestToolResultDataFromEventMapping:
    """Test that ToolResultData maps correctly from AgentEvent fields."""

    def test_from_tool_execution_end_success(self):
        """ToolResultData maps from successful tool_execution_end."""
        # Simulating mapping from AgentEvent:
        # type="tool_execution_end", tool_name="bash", tool_call_id="call_1", result="output"
        tr = ToolResultData(
            tool_name="bash",
            tool_call_id="call_1",
            result="output",
            is_error=False,
        )
        assert tr.tool_name == "bash"
        assert tr.tool_call_id == "call_1"
        assert tr.result == "output"
        assert tr.is_error is False

    def test_from_tool_execution_end_error(self):
        """ToolResultData maps from failed tool_execution_end."""
        tr = ToolResultData(
            tool_name="bash",
            tool_call_id="call_1",
            result="Error: exit code 1",
            is_error=True,
        )
        assert tr.is_error is True

    def test_tool_result_corresponds_to_tool_call(self):
        """ToolResultData.tool_call_id matches the ToolCallData.tool_call_id."""
        # Simulating the event flow:
        # 1. tool_execution_start → ToolCallData
        # 2. tool_execution_end → ToolResultData
        tc_call_id = "call_matching_123"
        tc_tool_name = "grep"

        tr = ToolResultData(
            tool_name=tc_tool_name,
            tool_call_id=tc_call_id,
            result="grep output",
        )
        assert tr.tool_call_id == tc_call_id
        assert tr.tool_name == tc_tool_name

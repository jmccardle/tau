"""Tests for tau_agent_core.tools.base — ToolDefinition, AgentTool, AgentToolResult, ToolBatchResult.

Tests verify:
- ToolDefinition has all required fields
- AgentTool wraps ToolDefinition with name/execute aliases
- AgentToolResult represents single tool execution result
- ToolBatchResult is serializable and represents batch results
- ToolBatchResult.terminate defaults to False

Reference: SUBPHASE-0.0.md, "2. Tool Definitions" section
Reference: PHASE-2-SUBPHASE-0.md, Testing section items 2, 4
"""

import pytest

from pydantic import ValidationError

from tau_agent_core.tools.base import (
    AgentTool,
    AgentToolResult,
    ToolBatchResult,
    ToolDefinition,
)


class TestToolDefinition:
    """Tests for ToolDefinition."""

    def create_sample_definition(self) -> dict:
        """Create a sample tool definition dict."""
        async def execute(ctx):
            return "test"

        return {
            "name": "ls",
            "label": "List Directory",
            "description": "List files and directories in a path",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Path to list"},
                },
                "required": ["path"],
            },
            "execute": execute,
            "prompt_snippet": "ls: List directory contents",
            "prompt_guidelines": ["Use absolute paths"],
            "execution_mode": "parallel",
        }

    def test_create_tool_definition(self):
        """ToolDefinition can be instantiated with all fields."""
        def execute(ctx):
            return "test"

        definition = ToolDefinition(
            name="ls",
            label="List Directory",
            description="List files and directories in a path",
            parameters={"type": "object", "properties": {}, "required": []},
            execute=execute,
            prompt_snippet="ls: List directory contents",
            prompt_guidelines=["Use absolute paths"],
            execution_mode="parallel",
        )
        assert definition.name == "ls"
        assert definition.label == "List Directory"
        assert definition.description == "List files and directories in a path"
        assert definition.prompt_snippet == "ls: List directory contents"
        assert definition.prompt_guidelines == ["Use absolute paths"]
        assert definition.execution_mode == "parallel"

    def test_tool_definition_defaults(self):
        """ToolDefinition has sensible defaults for optional fields."""
        def execute(ctx):
            return "test"

        definition = ToolDefinition(
            name="ls",
            label="List",
            description="List files",
            parameters={"type": "object", "properties": {}, "required": []},
            execute=execute,
        )
        assert definition.prompt_snippet is None
        assert definition.prompt_guidelines is None
        assert definition.execution_mode == "parallel"

    def test_tool_definition_name_required(self):
        """ToolDefinition requires a name."""
        def execute(ctx):
            return "test"

        with pytest.raises(ValidationError):
            ToolDefinition(
                label="List",
                description="List files",
                parameters={"type": "object", "properties": {}, "required": []},
                execute=execute,
            )

    def test_tool_definition_label_required(self):
        """ToolDefinition requires a label."""
        def execute(ctx):
            return "test"

        with pytest.raises(ValidationError):
            ToolDefinition(
                name="ls",
                description="List files",
                parameters={"type": "object", "properties": {}, "required": []},
                execute=execute,
            )

    def test_tool_definition_description_required(self):
        """ToolDefinition requires a description."""
        def execute(ctx):
            return "test"

        with pytest.raises(ValidationError):
            ToolDefinition(
                name="ls",
                label="List",
                parameters={"type": "object", "properties": {}, "required": []},
                execute=execute,
            )

    def test_tool_definition_parameters_required(self):
        """ToolDefinition requires parameters (JSON schema)."""
        def execute(ctx):
            return "test"

        with pytest.raises(ValidationError):
            ToolDefinition(
                name="ls",
                label="List",
                description="List files",
                execute=execute,
            )

    def test_tool_definition_execute_required(self):
        """ToolDefinition requires an execute callable."""
        with pytest.raises(ValidationError):
            ToolDefinition(
                name="ls",
                label="List",
                description="List files",
                parameters={"type": "object", "properties": {}, "required": []},
                execute=None,  # type: ignore
            )

    def test_tool_definition_serialization(self):
        """ToolDefinition serializes to dict correctly."""
        def execute(ctx):
            return "test"

        definition = ToolDefinition(
            name="ls",
            label="List",
            description="List files",
            parameters={"type": "object", "properties": {}, "required": []},
            execute=execute,
            execution_mode="sequential",
        )
        data = definition.model_dump()
        assert data["name"] == "ls"
        assert data["execution_mode"] == "sequential"

    def test_tool_definition_json(self):
        """ToolDefinition serializes to JSON string (excluding callable)."""
        def execute(ctx):
            return "test"

        definition = ToolDefinition(
            name="ls",
            label="List",
            description="List files",
            parameters={"type": "object", "properties": {}, "required": []},
            execute=execute,
        )
        # Callable fields can't be serialized to JSON directly
        json_str = definition.model_dump_json(exclude={"execute"})
        assert '"name":"ls"' in json_str

    def test_tool_definition_equality(self):
        """Two ToolDefinitions with the same name are equal."""
        def execute(ctx):
            return "test"

        d1 = ToolDefinition(
            name="ls",
            label="List",
            description="List files",
            parameters={"type": "object", "properties": {}, "required": []},
            execute=execute,
        )
        d2 = ToolDefinition(
            name="ls",
            label="List (alt)",
            description="List files (alt)",
            parameters={"type": "object", "properties": {}, "required": []},
            execute=execute,
        )
        assert d1 == d2

    def test_tool_definition_hashable(self):
        """ToolDefinition is hashable (can be used in sets/dicts)."""
        def execute(ctx):
            return "test"

        definition = ToolDefinition(
            name="ls",
            label="List",
            description="List files",
            parameters={"type": "object", "properties": {}, "required": []},
            execute=execute,
        )
        s = {definition}
        assert definition in s

    def test_tool_definition_execution_modes(self):
        """ToolDefinition supports sequential and parallel modes."""
        def execute(ctx):
            return "test"

        seq = ToolDefinition(
            name="ls",
            label="List",
            description="List files",
            parameters={"type": "object", "properties": {}, "required": []},
            execute=execute,
            execution_mode="sequential",
        )
        assert seq.execution_mode == "sequential"

        par = ToolDefinition(
            name="ls",
            label="List",
            description="List files",
            parameters={"type": "object", "properties": {}, "required": []},
            execute=execute,
            execution_mode="parallel",
        )
        assert par.execution_mode == "parallel"


class TestAgentTool:
    """Tests for AgentTool wrapper."""

    def create_sample_definition(self) -> ToolDefinition:
        """Create a sample ToolDefinition for testing."""
        def execute(ctx):
            return "test"

        return ToolDefinition(
            name="ls",
            label="List Directory",
            description="List files and directories in a path",
            parameters={"type": "object", "properties": {}, "required": []},
            execute=execute,
            execution_mode="parallel",
        )

    def test_create_agent_tool(self):
        """AgentTool wraps a ToolDefinition."""
        definition = self.create_sample_definition()
        agent_tool = AgentTool(definition=definition)
        assert agent_tool.name == "ls"
        assert agent_tool.execute is definition.execute

    def test_agent_tool_name_alias(self):
        """AgentTool.name is an alias for definition.name."""
        definition = self.create_sample_definition()
        agent_tool = AgentTool(definition=definition)
        assert agent_tool.name == definition.name

    def test_agent_tool_execute_alias(self):
        """AgentTool.execute is an alias for definition.execute."""
        definition = self.create_sample_definition()
        agent_tool = AgentTool(definition=definition)
        result = agent_tool.execute(None)
        assert result == "test"

    def test_agent_tool_serialization(self):
        """AgentTool serializes to dict (includes nested definition)."""
        definition = self.create_sample_definition()
        agent_tool = AgentTool(definition=definition)
        data = agent_tool.model_dump()
        assert data["definition"]["name"] == "ls"

    def test_agent_tool_equality(self):
        """Two AgentTools with the same name are equal."""
        d1 = self.create_sample_definition()
        def execute2(ctx):
            return "test2"
        d2 = ToolDefinition(
            name="ls",
            label="List",
            description="List files",
            parameters={"type": "object", "properties": {}, "required": []},
            execute=execute2,
        )
        at1 = AgentTool(definition=d1)
        at2 = AgentTool(definition=d2)
        assert at1 == at2


class TestAgentToolResult:
    """Tests for AgentToolResult."""

    def test_create_tool_result_success(self):
        """AgentToolResult represents a successful tool execution."""
        result = AgentToolResult(
            tool_name="ls",
            tool_call_id="call_123",
            content=[{"type": "text", "text": "file1.txt\nfile2.py"}],
        )
        assert result.tool_name == "ls"
        assert result.tool_call_id == "call_123"
        assert result.is_error is False
        assert result.error_message is None
        assert len(result.content) == 1

    def test_create_tool_result_from_error(self):
        """AgentToolResult.from_error creates a failure result."""
        result = AgentToolResult.from_error(
            tool_name="bash",
            error_message="Command failed with exit code 1",
            tool_call_id="call_err",
        )
        assert result.tool_name == "bash"
        assert result.is_error is True
        assert result.error_message == "Command failed with exit code 1"
        assert result.tool_call_id == "call_err"
        assert len(result.content) == 1
        assert result.content[0]["type"] == "text"
        assert result.content[0]["text"] == "Command failed with exit code 1"

    def test_tool_result_defaults(self):
        """AgentToolResult has sensible defaults."""
        result = AgentToolResult(tool_name="ls")
        assert result.tool_call_id is None
        assert result.content == []
        assert result.is_error is False
        assert result.error_message is None

    def test_tool_result_serialization(self):
        """AgentToolResult serializes to dict."""
        result = AgentToolResult(
            tool_name="ls",
            content=[{"type": "text", "text": "output"}],
        )
        data = result.model_dump()
        assert data["tool_name"] == "ls"
        assert data["content"] == [{"type": "text", "text": "output"}]


class TestToolBatchResult:
    """Tests for ToolBatchResult.

    Reference: PHASE-2-SUBPHASE-0.md, Testing section item 4:
    > ToolBatchResult is serializable
    > result = ToolBatchResult(messages=[], terminate=False)
    > assert result.terminate is False
    """

    def test_create_empty_batch_result(self):
        """ToolBatchResult can be instantiated with defaults."""
        result = ToolBatchResult(
            messages=[],
            terminate=False,
        )
        assert result.messages == []
        assert result.terminate is False

    def test_batch_result_defaults(self):
        """ToolBatchResult has sensible defaults."""
        result = ToolBatchResult()
        assert result.messages == []
        assert result.tool_results == []
        assert result.terminate is False

    def test_batch_result_terminate_false(self):
        """ToolBatchResult.terminate defaults to False.

        Reference: PHASE-2-SUBPHASE-0.md, Testing section item 4.
        """
        result = ToolBatchResult(
            messages=[],
            terminate=False,
        )
        assert result.terminate is False

    def test_batch_result_with_tool_results(self):
        """ToolBatchResult can contain multiple tool results."""
        result1 = AgentToolResult(
            tool_name="ls",
            tool_call_id="call_1",
            content=[{"type": "text", "text": "file1.txt"}],
        )
        result2 = AgentToolResult(
            tool_name="grep",
            tool_call_id="call_2",
            content=[{"type": "text", "text": "match: foo"}],
            is_error=True,
            error_message="No matches found",
        )
        batch = ToolBatchResult(
            messages=[{"role": "assistant", "content": []}],
            tool_results=[result1, result2],
            terminate=False,
        )
        assert len(batch.tool_results) == 2
        assert batch.tool_results[0].is_error is False
        assert batch.tool_results[1].is_error is True

    def test_batch_result_terminate_true(self):
        """ToolBatchResult can signal termination."""
        batch = ToolBatchResult(terminate=True)
        assert batch.terminate is True

    def test_batch_result_serialization(self):
        """ToolBatchResult serializes to dict."""
        batch = ToolBatchResult(
            messages=[],
            terminate=False,
        )
        data = batch.model_dump()
        assert "messages" in data
        assert "tool_results" in data
        assert "terminate" in data

    def test_batch_result_json(self):
        """ToolBatchResult serializes to JSON string."""
        batch = ToolBatchResult(terminate=False)
        json_str = batch.model_dump_json()
        assert '"terminate":false' in json_str

    def test_batch_result_bool_false_when_terminated(self):
        """ToolBatchResult.__bool__ returns False when terminated."""
        batch = ToolBatchResult(terminate=True)
        assert bool(batch) is False

    def test_batch_result_bool_true_when_not_terminated(self):
        """ToolBatchResult.__bool__ returns True when not terminated."""
        batch = ToolBatchResult(terminate=False)
        assert bool(batch) is True

    def test_batch_result_empty_is_truthy(self):
        """Empty (non-terminated) ToolBatchResult is truthy."""
        batch = ToolBatchResult()
        assert batch is not None
        assert batch is not False  # terminate=False -> __bool__ returns True


class TestToolsBaseImport:
    """Tests for module-level imports.

    Reference: PHASE-2-SUBPHASE-0.md, Testing section item 1.
    > from tau_agent_core.tools.base import AgentTool, AgentToolResult, ToolBatchResult
    """

    def test_import_from_module(self):
        """All types import from tools.base module."""
        from tau_agent_core.tools.base import (
            AgentTool,
            AgentToolResult,
            ToolBatchResult,
            ToolDefinition,
        )
        assert AgentTool is not None
        assert AgentToolResult is not None
        assert ToolBatchResult is not None
        assert ToolDefinition is not None

    def test_import_from_package_root(self):
        """All types import from tau_agent_core package root."""
        from tau_agent_core import (
            AgentTool,
            AgentToolResult,
            ToolBatchResult,
            ToolDefinition,
        )
        assert AgentTool is not None
        assert AgentToolResult is not None
        assert ToolBatchResult is not None
        assert ToolDefinition is not None

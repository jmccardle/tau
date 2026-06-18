"""Tests for tau_agent_core.agent_loop_types — PreparedToolCall, FinalizedToolCall, AgentLoopConfig.

Tests verify:
- PreparedToolCall: Created from LLM response, has id/name/arguments
- FinalizedToolCall: Created after execution, includes result and is_error
- AgentLoopConfig: Has all optional fields with correct defaults
- All types are serializable (Pydantic models)

Reference: SUBPHASE-0.0.md, "Agent Loop Types" section
Reference: PHASE-2-SUBPHASE-0.md, Testing section items 4-5
"""

import pytest

from pydantic import ValidationError

from tau_agent_core.agent_loop_types import (
    AgentLoopConfig,
    FinalizedToolCall,
    PreparedToolCall,
)


class TestPreparedToolCall:
    """Tests for PreparedToolCall."""

    def test_create_prepared_tool_call(self):
        """PreparedToolCall can be instantiated with id, name, arguments."""
        call = PreparedToolCall(
            id="call_abc123",
            name="ls",
            arguments={"path": "."},
        )
        assert call.id == "call_abc123"
        assert call.name == "ls"
        assert call.arguments == {"path": "."}

    def test_prepared_tool_call_with_empty_args(self):
        """PreparedToolCall can be created with empty arguments."""
        call = PreparedToolCall(
            id="call_xyz",
            name="ls",
            arguments={},
        )
        assert call.arguments == {}

    def test_prepared_tool_call_serialization(self):
        """PreparedToolCall serializes to dict correctly."""
        call = PreparedToolCall(
            id="call_1",
            name="grep",
            arguments={"pattern": "foo", "path": "/tmp"},
        )
        data = call.model_dump()
        assert data["id"] == "call_1"
        assert data["name"] == "grep"
        assert data["arguments"]["pattern"] == "foo"

    def test_prepared_tool_call_json(self):
        """PreparedToolCall serializes to JSON string."""
        call = PreparedToolCall(
            id="call_1",
            name="ls",
            arguments={"path": "/tmp"},
        )
        json_str = call.model_dump_json()
        assert '"id":"call_1"' in json_str
        assert '"name":"ls"' in json_str

    def test_prepared_tool_call_id_required(self):
        """PreparedToolCall requires an id."""
        with pytest.raises(ValidationError):
            PreparedToolCall(
                name="ls",
                arguments={"path": "."},
            )

    def test_prepared_tool_call_name_required(self):
        """PreparedToolCall requires a name."""
        with pytest.raises(ValidationError):
            PreparedToolCall(
                id="call_1",
                arguments={"path": "."},
            )

    def test_prepared_tool_call_empty_arguments(self):
        """PreparedToolCall accepts empty arguments dict."""
        call = PreparedToolCall(
            id="call_1",
            name="ls",
            arguments={},
        )
        assert call.arguments == {}


class TestFinalizedToolCall:
    """Tests for FinalizedToolCall."""

    def test_create_finalized_tool_call(self):
        """FinalizedToolCall can be instantiated with result."""
        call = FinalizedToolCall(
            id="call_abc123",
            name="ls",
            arguments={"path": "."},
            result="file1.txt\nfile2.py",
        )
        assert call.id == "call_abc123"
        assert call.name == "ls"
        assert call.result == "file1.txt\nfile2.py"
        assert call.is_error is False

    def test_finalized_tool_call_with_error(self):
        """FinalizedToolCall can represent an error."""
        call = FinalizedToolCall(
            id="call_err",
            name="bash",
            arguments={"command": "exit 1"},
            result="Error: exit 1",
            is_error=True,
        )
        assert call.is_error is True

    def test_finalized_tool_call_defaults(self):
        """FinalizedToolCall has sensible defaults."""
        call = FinalizedToolCall(
            id="call_1",
            name="ls",
            arguments={"path": "."},
        )
        assert call.result is None
        assert call.is_error is False

    def test_finalized_tool_call_serialization(self):
        """FinalizedToolCall serializes to dict correctly."""
        call = FinalizedToolCall(
            id="call_1",
            name="grep",
            arguments={"pattern": "foo"},
            result=["match1", "match2"],
            is_error=False,
        )
        data = call.model_dump()
        assert data["result"] == ["match1", "match2"]

    def test_finalized_tool_call_json(self):
        """FinalizedToolCall serializes to JSON string."""
        call = FinalizedToolCall(
            id="call_1",
            name="ls",
            arguments={},
            result="output",
        )
        json_str = call.model_dump_json()
        assert '"result":"output"' in json_str

    def test_finalized_tool_call_from_prepared(self):
        """FinalizedToolCall can be created from a PreparedToolCall."""
        prepared = PreparedToolCall(
            id="call_1",
            name="ls",
            arguments={"path": "."},
        )
        finalized = FinalizedToolCall(
            id=prepared.id,
            name=prepared.name,
            arguments=prepared.arguments,
            result="file1.txt",
        )
        assert finalized.id == prepared.id
        assert finalized.name == prepared.name
        assert finalized.arguments == prepared.arguments


class TestAgentLoopConfig:
    """Tests for AgentLoopConfig.

    Reference: PHASE-2-SUBPHASE-0.md, Testing section item 5:
    > AgentLoopConfig has all optional fields
    > config.max_retries == 3  # default
    """

    def test_create_agent_loop_config(self):
        """AgentLoopConfig can be instantiated with minimal config."""
        config = AgentLoopConfig()
        assert config.tool_execution_mode == "parallel"
        assert config.max_retries == 3
        assert config.max_turns == 50
        assert config.temperature == 0.7

    def test_create_agent_loop_config_with_all_fields(self):
        """AgentLoopConfig accepts all fields."""
        config = AgentLoopConfig(
            model="gpt-4o",
            system_prompt="You are a helpful assistant.",
            tool_execution_mode="sequential",
            max_retries=5,
            max_turns=10,
            temperature=0.5,
        )
        assert config.model == "gpt-4o"
        assert config.system_prompt == "You are a helpful assistant."
        assert config.tool_execution_mode == "sequential"
        assert config.max_retries == 5
        assert config.max_turns == 10
        assert config.temperature == 0.5

    def test_agent_loop_config_default_max_retries_is_3(self):
        """AgentLoopConfig.max_retries defaults to 3.

        Reference: PHASE-2-SUBPHASE-0.md, Testing section item 5.
        """
        config = AgentLoopConfig(
            model="gpt-4o",
            system_prompt="test",
            tool_execution_mode="parallel",
        )
        assert config.tool_execution_mode == "parallel"
        assert config.max_retries == 3  # default

    def test_agent_loop_config_serialization(self):
        """AgentLoopConfig serializes to dict correctly."""
        config = AgentLoopConfig(
            model="claude-3",
            system_prompt="test",
            tool_execution_mode="parallel",
        )
        data = config.model_dump()
        assert data["model"] == "claude-3"
        assert data["tool_execution_mode"] == "parallel"
        assert data["max_retries"] == 3

    def test_agent_loop_config_json(self):
        """AgentLoopConfig serializes to JSON string."""
        config = AgentLoopConfig(model="gpt-4o")
        json_str = config.model_dump_json()
        assert '"model":"gpt-4o"' in json_str

    def test_tool_execution_mode_sequential(self):
        """AgentLoopConfig supports sequential mode."""
        config = AgentLoopConfig(tool_execution_mode="sequential")
        assert config.tool_execution_mode == "sequential"

    def test_tool_execution_mode_parallel(self):
        """AgentLoopConfig supports parallel mode."""
        config = AgentLoopConfig(tool_execution_mode="parallel")
        assert config.tool_execution_mode == "parallel"

    def test_max_retries_minimum_zero(self):
        """AgentLoopConfig.max_retries >= 0."""
        config = AgentLoopConfig(max_retries=0)
        assert config.max_retries == 0

    def test_max_retries_rejects_negative(self):
        """AgentLoopConfig.max_retries rejects negative values."""
        with pytest.raises(ValidationError):
            AgentLoopConfig(max_retries=-1)

    def test_max_turns_minimum_one(self):
        """AgentLoopConfig.max_turns >= 1."""
        config = AgentLoopConfig(max_turns=1)
        assert config.max_turns == 1

    def test_max_turns_rejects_zero(self):
        """AgentLoopConfig.max_turns rejects zero."""
        with pytest.raises(ValidationError):
            AgentLoopConfig(max_turns=0)

    def test_temperature_bounds(self):
        """AgentLoopConfig.temperature must be between 0.0 and 2.0."""
        config_min = AgentLoopConfig(temperature=0.0)
        assert config_min.temperature == 0.0

        config_max = AgentLoopConfig(temperature=2.0)
        assert config_max.temperature == 2.0

    def test_temperature_rejects_out_of_bounds(self):
        """AgentLoopConfig.temperature rejects values outside [0, 2]."""
        with pytest.raises(ValidationError):
            AgentLoopConfig(temperature=-0.1)
        with pytest.raises(ValidationError):
            AgentLoopConfig(temperature=2.1)


class TestAgentLoopTypesImport:
    """Tests for module-level imports.

    Reference: PHASE-2-SUBPHASE-0.md, Testing section item 1.
    > from tau_agent_core.agent_loop_types import PreparedToolCall, FinalizedToolCall, AgentLoopConfig
    """

    def test_import_from_module(self):
        """All types import from agent_loop_types module."""
        from tau_agent_core.agent_loop_types import (
            PreparedToolCall,
            FinalizedToolCall,
            AgentLoopConfig,
        )
        assert PreparedToolCall is not None
        assert FinalizedToolCall is not None
        assert AgentLoopConfig is not None

    def test_import_from_package_root(self):
        """All types import from tau_agent_core package root."""
        from tau_agent_core import (
            PreparedToolCall,
            FinalizedToolCall,
            AgentLoopConfig,
        )
        assert PreparedToolCall is not None
        assert FinalizedToolCall is not None
        assert AgentLoopConfig is not None

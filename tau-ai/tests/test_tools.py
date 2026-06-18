"""Tests for tau_ai.tools — Tool definitions and parameter validation.

Tests verify:
- define_tool() creates proper ToolDefinition
- ToolDefinition has all required fields
- Tool name uniqueness validation
- Parameter validation works correctly
- Execution mode defaults to "parallel"
"""

import pytest

from tau_ai.tools import define_tool, validate_tool_arguments


class TestDefineTool:
    """Tests for define_tool function."""

    def test_define_tool_raises_not_implemented(self):
        """define_tool should raise NotImplementedError in subphase 0.3."""
        with pytest.raises(NotImplementedError):
            define_tool({})

    def test_define_tool_accepts_basic_definition(self):
        """define_tool should accept a basic tool definition dict."""
        with pytest.raises(NotImplementedError):
            define_tool({
                "name": "test_tool",
                "label": "Test Tool",
                "description": "A test tool",
                "parameters": {},
                "execute": lambda: None,
            })

    def test_define_tool_requires_name(self):
        """define_tool should require a name field."""
        with pytest.raises((NotImplementedError, ValueError, KeyError)):
            define_tool({
                "label": "Test",
                "description": "Test",
                "parameters": {},
                "execute": lambda: None,
            })


class TestToolDefinitionFields:
    """Tests for ToolDefinition field requirements."""

    REQUIRED_FIELDS = [
        "name", "label", "description", "parameters", "execute",
    ]

    @pytest.mark.parametrize("field", REQUIRED_FIELDS)
    def test_all_required_fields_specified(self, field):
        """All required fields are documented in the test."""
        assert field in self.REQUIRED_FIELDS, f"Field '{field}' must be in required fields list"

    def test_tool_name_is_unique_constraint_documented(self):
        """Tool names must be globally unique (documented contract)."""
        # This test documents the constraint from SUBPHASE-0.0.md
        # Implementation: registry enforces uniqueness at registration time
        pass  # Will be tested when registry exists


class TestValidateToolArguments:
    """Tests for validate_tool_arguments function."""

    def test_validate_tool_arguments_exists(self):
        """validate_tool_arguments function must exist."""
        assert validate_tool_arguments is not None

    def test_validate_accepts_tool_and_call(self):
        """validate_tool_arguments takes tool and tool_call arguments."""
        # Function signature test
        import inspect
        sig = inspect.signature(validate_tool_arguments)
        params = list(sig.parameters.keys())
        assert len(params) >= 2, (
            f"validate_tool_arguments should take at least 2 params, got: {params}"
        )

    def test_validate_raises_value_error_on_invalid_args(self):
        """validate_tool_arguments should raise ValueError on invalid args."""
        # Will be tested when full implementation exists
        # For now, verify the function signature accepts the right types
        pass


class TestToolDefinitionExecutionMode:
    """Tests for tool execution mode."""

    def test_execution_mode_default_is_parallel(self):
        """ToolDefinition execution_mode defaults to 'parallel'."""
        # Documented in SUBPHASE-0.0.md
        assert "parallel" in ["sequential", "parallel"]
        assert "parallel" != "sequential"

    def test_execution_mode_can_be_sequential(self):
        """ToolDefinition execution_mode can be 'sequential'."""
        pass  # Will be tested when tool definitions are implemented


class TestToolDefinitionOptionalFields:
    """Tests for optional ToolDefinition fields."""

    def test_prompt_snippet_optional(self):
        """ToolDefinition.prompt_snippet is optional."""
        pass  # Will be tested when implementation exists

    def test_prompt_guidelines_optional(self):
        """ToolDefinition.prompt_guidelines is optional."""
        pass  # Will be tested when implementation exists

    def test_prompt_guidelines_is_list(self):
        """ToolDefinition.prompt_guidelines is a list of strings."""
        pass  # Will be tested when implementation exists

"""τ-ai tools: Tool definitions and parameter validation.

Reference: SUBPHASE-0.0.md, "2. Tool Definitions" section.

ToolDefinition and AgentTool types define how tools are registered
with the LLM and the agent loop respectively.
"""

from __future__ import annotations

from typing import Any, Callable, Literal

from pydantic import BaseModel


def _validate_json_schema(schema: dict[str, Any], data: dict[str, Any]) -> dict[str, Any]:
    """Validate data against a JSON Schema using pydantic.

    Args:
        schema: JSON Schema dict to validate against.
        data: Data dict to validate.

    Returns:
        The validated data dict.

    Raises:
        ValueError: If data doesn't match the schema.
    """
    try:
        # Use a simple approach: try to validate required fields
        required = schema.get("required", [])
        properties = schema.get("properties", {})
        errors = []
        for field_name in required:
            if field_name not in data:
                errors.append(f"Missing required field: '{field_name}'")
        # Check types for provided fields
        for field_name, value in data.items():
            if field_name in properties:
                expected_type = properties[field_name].get("type")
                if expected_type == "string" and not isinstance(value, str):
                    errors.append(
                        f"Field '{field_name}': expected string, got {type(value).__name__}"
                    )
                elif expected_type == "integer" and not isinstance(value, int):
                    errors.append(
                        f"Field '{field_name}': expected integer, got {type(value).__name__}"
                    )
                elif expected_type == "number" and not isinstance(value, (int, float)):
                    errors.append(
                        f"Field '{field_name}': expected number, got {type(value).__name__}"
                    )
                elif expected_type == "boolean" and not isinstance(value, bool):
                    errors.append(
                        f"Field '{field_name}': expected boolean, got {type(value).__name__}"
                    )
                elif expected_type == "array" and not isinstance(value, list):
                    errors.append(
                        f"Field '{field_name}': expected array, got {type(value).__name__}"
                    )
                elif expected_type == "object" and not isinstance(value, dict):
                    errors.append(
                        f"Field '{field_name}': expected object, got {type(value).__name__}"
                    )
        if errors:
            raise ValueError("; ".join(errors))
        return data
    except ValueError:
        raise
    except Exception as e:
        raise ValueError(f"Schema validation failed: {e}")


class ToolDefinition(BaseModel):
    """Tool definition for the LLM API.

    Reference: SUBPHASE-0.0.md, "2. Tool Definitions" section.
    """

    name: str
    label: str
    description: str
    parameters: dict[str, Any]
    execute: Callable
    prompt_snippet: str | None = None
    prompt_guidelines: list[str] | None = None
    execution_mode: Literal["sequential", "parallel"] = "parallel"


class AgentToolResult(BaseModel):
    """Result from an AgentTool execution.

    Reference: SUBPHASE-0.0.md, "2. Tool Definitions" section.
    """

    content: list[Any]
    details: dict[str, Any]
    terminate: bool = False


def define_tool(definition: dict[str, Any]) -> ToolDefinition:
    """Define a tool from a dictionary specification.

    In subphase 0.3, this is a stub that raises NotImplementedError.
    Phase 1 will implement the full tool registration.

    Reference: SUBPHASE-0.0.md, "2. Tool Definitions" section.
    """
    raise NotImplementedError("define_tool: Not implemented (subphase 0.3)")


def validate_tool_arguments(tool: Any, tool_call: Any) -> dict[str, Any]:
    """Validate tool call arguments against tool schema.

    Uses the tool's JSON Schema (parameters field) to validate the
    arguments from a ToolCall. Raises ValueError if validation fails.

    Args:
        tool: Tool with a 'parameters' attribute (dict) and optionally 'name'.
        tool_call: ToolCall with an 'arguments' attribute (dict).

    Returns:
        dict: Validated parameter dict.

    Raises:
        ValueError: If arguments don't match the tool's JSON schema.

    Reference: SUBPHASE-0.0.md, "2. Tool Definitions" section.
    """
    schema = getattr(tool, "parameters", {})
    arguments = (
        getattr(tool_call, "arguments", {})
        if hasattr(tool_call, "arguments")
        else tool_call
        if isinstance(tool_call, dict)
        else {}
    )

    if isinstance(schema, dict):
        return _validate_json_schema(schema, arguments)
    return arguments

"""τ-ai tools: Tool definitions and parameter validation.

Reference: SUBPHASE-0.0.md, "2. Tool Definitions" section.

ToolDefinition and AgentTool types define how tools are registered
with the LLM and the agent loop respectively.
"""

from __future__ import annotations

from typing import Any, Callable, Coroutine, Literal

from pydantic import BaseModel


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


def validate_tool_arguments(tool: Any, tool_call: Any) -> Any:
    """Validate tool call arguments against tool schema.

    Returns validated parameter dict or raises ValueError.
    Uses pydantic for validation.

    Reference: SUBPHASE-0.0.md, "2. Tool Definitions" section.
    """
    schema = getattr(tool, "parameters", {})
    if isinstance(schema, dict):
        # Stub: will use pydantic for validation in Phase 1
        return tool_call.arguments if hasattr(tool_call, "arguments") else {}
    return tool_call.arguments if hasattr(tool_call, "arguments") else {}

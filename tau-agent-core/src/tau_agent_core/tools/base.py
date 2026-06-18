"""τ-agent-core tools: Tool definitions, AgentTool wrapper, and batch results.

Reference: SUBPHASE-0.0.md, "2. Tool Definitions" section.

Types:
- ToolDefinition: Raw tool definition (from tau-ai or extensions)
- AgentTool: Validated tool wrapper used by the agent loop
- AgentToolResult: Result from a single tool execution
- ToolBatchResult: Result from a batch of tool executions

Constraints:
- Tool names must be globally unique across all sources
- Tool arguments are validated against JSON Schema
"""

from __future__ import annotations

from typing import Any, Callable, Literal

from pydantic import BaseModel, Field


class ToolDefinition(BaseModel):
    """Raw tool definition from tau-ai or extensions.

    Reference: SUBPHASE-0.0.md, "2. Tool Definitions" section.

    Attributes:
        name: Unique, snake_case tool name
        label: Human-readable name (for TUI)
        description: Tool description sent to LLM
        parameters: JSON Schema dict for argument validation
        execute: Async callable for tool execution
        prompt_snippet: One-line summary for system prompt
        prompt_guidelines: Guidelines for LLM usage
        execution_mode: "sequential" or "parallel"
    """

    name: str
    label: str
    description: str
    parameters: dict[str, Any]
    execute: Callable[..., Any]
    prompt_snippet: str | None = None
    prompt_guidelines: list[str] | None = None
    execution_mode: Literal["sequential", "parallel"] = "parallel"

    def __hash__(self) -> int:
        return hash(self.name)

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, ToolDefinition):
            return False
        return self.name == other.name


class AgentTool(BaseModel):
    """Validated tool wrapper used by the agent loop.

    Reference: SUBPHASE-0.0.md, "2. Tool Definitions" section.

    Wraps a ToolDefinition with validated name alias.
    The agent loop works with AgentTool (validated, wrapped),
    while extensions register with ToolDefinition (raw, unvalidated).

    Attributes:
        definition: The underlying ToolDefinition
        name: Alias for definition.name
        execute: Alias for definition.execute
    """

    definition: ToolDefinition

    @property
    def name(self) -> str:
        """Alias for definition.name."""
        return self.definition.name

    @property
    def execute(self) -> Callable[..., Any]:
        """Alias for definition.execute."""
        return self.definition.execute

    def __hash__(self) -> int:
        return hash(self.name)

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, AgentTool):
            return False
        return self.name == other.name


class AgentToolResult(BaseModel):
    """Result from a single tool execution.

    Attributes:
        tool_name: Name of the executed tool
        tool_call_id: ID of the tool call
        content: List of content blocks (mirrors Message content)
        is_error: Whether the execution failed
        error_message: Error description (if is_error=True)
    """

    tool_name: str
    tool_call_id: str | None = None
    content: list[dict[str, Any]] = Field(default_factory=list)
    is_error: bool = False
    error_message: str | None = None

    @classmethod
    def from_error(cls, tool_name: str, error_message: str, tool_call_id: str | None = None) -> "AgentToolResult":
        """Create a failure result."""
        return cls(
            tool_name=tool_name,
            tool_call_id=tool_call_id,
            content=[{"type": "text", "text": error_message}],
            is_error=True,
            error_message=error_message,
        )


class ToolBatchResult(BaseModel):
    """Result from a batch of tool executions.

    Returned by the agent loop after executing a batch of tool calls.

    Attributes:
        messages: List of messages produced by the tool executions
        tool_results: Individual tool execution results
        terminate: Whether the agent loop should terminate
    """

    messages: list[dict[str, Any]] = Field(default_factory=list)
    tool_results: list[AgentToolResult] = Field(default_factory=list)
    terminate: bool = False

    def __bool__(self) -> bool:
        return not self.terminate

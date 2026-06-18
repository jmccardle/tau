"""τ-agent-core tools: Tool definitions and base types.

Exports:
- ToolDefinition: Raw tool definition
- AgentTool: Validated tool wrapper
- AgentToolResult: Result from a single tool execution
- ToolBatchResult: Result from a batch of tool executions

Reference: SUBPHASE-0.0.md, "2. Tool Definitions" section.
"""

from tau_agent_core.tools.base import (
    AgentTool,
    AgentToolResult,
    ToolBatchResult,
    ToolDefinition,
)

__all__ = [
    "AgentTool",
    "AgentToolResult",
    "ToolBatchResult",
    "ToolDefinition",
]

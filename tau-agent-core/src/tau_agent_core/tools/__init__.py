"""τ-agent-core tools: Tool definitions and base types.

Exports:
- ToolDefinition: Raw tool definition
- AgentTool: Validated tool wrapper
- AgentToolResult: Result from a single tool execution
- ToolBatchResult: Result from a batch of tool executions
- create_all_tools(cwd): Factory to create all built-in tools
- create_coding_tools(cwd): Factory for coding tools
- create_read_only_tools(cwd): Factory for read-only tools
- ReadTool, WriteTool, EditTool, BashTool, GrepTool, FindTool, LsTool

Reference: SUBPHASE-0.0.md, "2. Tool Definitions" section.
Reference: PHASE-2-SUBPHASE-3.md, tool definitions.
"""

from tau_agent_core.tools.base import (
    AgentTool,
    AgentToolResult,
    ToolBatchResult,
    ToolDefinition,
)
from tau_agent_core.tools.read import ReadTool
from tau_agent_core.tools.write import WriteTool
from tau_agent_core.tools.edit import EditTool
from tau_agent_core.tools.bash import BashTool
from tau_agent_core.tools.grep import GrepTool
from tau_agent_core.tools.find import FindTool
from tau_agent_core.tools.ls import LsTool


def create_all_tools(cwd: str) -> list:
    """Create all built-in tools for the given working directory.

    Returns:
        List of all 7 built-in tool instances.
    """
    return [
        ReadTool(cwd=cwd),
        WriteTool(cwd=cwd),
        EditTool(cwd=cwd),
        BashTool(cwd=cwd),
        GrepTool(cwd=cwd),
        FindTool(cwd=cwd),
        LsTool(cwd=cwd),
    ]


def create_coding_tools(cwd: str) -> list:
    """Create tools suitable for coding (includes all)."""
    return create_all_tools(cwd)


def create_read_only_tools(cwd: str) -> list:
    """Create read-only tools (no write, edit, or bash)."""
    return [
        ReadTool(cwd=cwd),
        GrepTool(cwd=cwd),
        FindTool(cwd=cwd),
        LsTool(cwd=cwd),
    ]


__all__ = [
    # Base types
    "AgentTool",
    "AgentToolResult",
    "ToolBatchResult",
    "ToolDefinition",
    # Tool classes
    "ReadTool",
    "WriteTool",
    "EditTool",
    "BashTool",
    "GrepTool",
    "FindTool",
    "LsTool",
    # Factory functions
    "create_all_tools",
    "create_coding_tools",
    "create_read_only_tools",
]

"""ToolResultData: Data contract for tool result widget.

This dataclass represents the data for a tool result widget in the TUI.
It maps from AgentEvent fields (tool_execution_end, result) to
widget-renderable data.

Reference: PHASE-4-SUBPHASE-0.md — ToolResultData Contract
Reference: SUBPHASE-0.0.md — AgentEvent.tool_execution_end fields
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class ToolResultData:
    """Data for a tool result widget.

    Maps from AgentEvent fields:
    - tool_name: extracted from AgentEvent.tool_name
    - tool_call_id: extracted from AgentEvent.tool_call_id
    - result: extracted from AgentEvent.result
    - is_error: extracted from AgentEvent.is_error

    Attributes:
        tool_name: Name of the tool that produced this result
        tool_call_id: Unique ID for the corresponding tool call
        result: The tool execution result (may be any serializable type)
        is_error: Whether this result represents an error
    """

    tool_name: str
    tool_call_id: str
    result: Any
    is_error: bool = False

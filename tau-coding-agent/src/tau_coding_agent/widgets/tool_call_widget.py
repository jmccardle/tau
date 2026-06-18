"""ToolCallData: Data contract for tool call widget.

This dataclass represents the data for a tool call widget in the TUI.
It maps from AgentEvent fields (tool_execution_start, tool_call_id, args)
to widget-renderable data.

Reference: PHASE-4-SUBPHASE-0.md — ToolCallData Contract
Reference: SUBPHASE-0.0.md — AgentEvent.tool_execution_* fields
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


@dataclass
class ToolCallData:
    """Data for a tool call widget.

    Maps from AgentEvent fields:
    - tool_name: extracted from AgentEvent.tool_name or tool definition
    - tool_call_id: extracted from AgentEvent.tool_call_id
    - arguments: extracted from AgentEvent.args
    - status: derived from event type (pending/running/done/error)
    - result_preview: extracted from AgentEvent.result (first N chars)

    Attributes:
        tool_name: Name of the tool being called
        tool_call_id: Unique ID for this tool call
        arguments: Tool call arguments (validated dict)
        status: Current status — "pending", "running", "done", or "error"
        result_preview: Short preview of the result (truncated if long)
    """

    tool_name: str
    tool_call_id: str
    arguments: dict
    status: Literal["pending", "running", "done", "error"]
    result_preview: str | None = None

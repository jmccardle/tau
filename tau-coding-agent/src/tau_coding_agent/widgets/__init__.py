"""τ-coding-agent widgets: TUI widget data classes.

This module defines the data contracts between τ-agent-core and the TUI widgets.
Each dataclass represents the data that a widget needs to render itself.

Reference: PHASE-4-SUBPHASE-0.md — Data Contract Definition
Reference: SUBPHASE-0.0.md — AgentEvent fields → widget data mapping
"""

from tau_coding_agent.widgets.chat_display import ChatMessageData
from tau_coding_agent.widgets.tool_call_widget import ToolCallData
from tau_coding_agent.widgets.tool_result_widget import ToolResultData
from tau_coding_agent.widgets.footer import FooterData

__all__ = [
    "ChatMessageData",
    "ToolCallData",
    "ToolResultData",
    "FooterData",
]

"""τ-coding-agent widgets: TUI widget data classes and Textual widgets.

This module defines both the data contracts and the Textual widget classes
for the TUI. Data classes map from AgentEvent fields to widget-renderable data.
Widget classes render that data using Textual components.

Reference: PHASE-4-SUBPHASE-2.md — Agent-Aware Widgets
Reference: SUBPHASE-0.0.md — AgentEvent fields → widget data mapping

Data contracts:
    ChatMessageData, ToolCallData, ToolResultData, FooterData

Textual widgets (when Textual is available):
    UserMessageWidget, AssistantMessageWidget, ThinkingBlockWidget,
    ToolCallWidget, ToolResultWidget, FooterWidget, ChatDisplay
"""

from tau_coding_agent.widgets.chat_display_data import ChatMessageData
from tau_coding_agent.widgets.tool_call_widget import ToolCallData
from tau_coding_agent.widgets.tool_result_widget import ToolResultData
from tau_coding_agent.widgets.footer import FooterData

# Session tree and input bar (always available)
from tau_coding_agent.widgets.session_tree import (
    SessionTreeWidget,
    SessionInfo,
)
from tau_coding_agent.widgets.input_bar import (
    InputBar,
    InputBarWidget,
    InputSubmitted,
)

# Re-export data classes from the main widgets namespace
__all__ = [
    "ChatMessageData",
    "ToolCallData",
    "ToolResultData",
    "FooterData",
    "SessionTreeWidget",
    "SessionInfo",
    "InputBar",
    "InputBarWidget",
    "InputSubmitted",
]

# Try to import Textual widgets (may fail if Textual not installed)
try:
    from tau_coding_agent.widgets.chat_display import (
        ChatDisplay,
        UserMessageWidget,
        AssistantMessageWidget,
        ThinkingBlockWidget,
    )
    from tau_coding_agent.widgets.tool_call_widget import ToolCallWidget
    from tau_coding_agent.widgets.tool_result_widget import ToolResultWidget
    from tau_coding_agent.widgets.footer import FooterWidget

    __all__ += [
        "ChatDisplay",
        "UserMessageWidget",
        "AssistantMessageWidget",
        "ThinkingBlockWidget",
        "ToolCallWidget",
        "ToolResultWidget",
        "FooterWidget",
    ]
except ImportError:
    # Textual not installed — widgets module not available
    pass

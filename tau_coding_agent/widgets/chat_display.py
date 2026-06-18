"""ChatMessageData: Data contract for chat message widget.

This dataclass represents the data passed to a chat message widget
in the TUI. It maps from AgentEvent fields (message content, tool info,
streaming state) to widget-renderable data.

Reference: PHASE-4-SUBPHASE-0.md — ChatMessageData Contract
Reference: SUBPHASE-0.0.md — AgentEvent.message field
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class ChatMessageData:
    """Data passed to a chat message widget.

    Maps from AgentEvent.message to TUI widget data:
    - role: extracted from message.role
    - content: list of ContentBlock dicts (text, thinking, image, toolCall)
    - timestamp: extracted from message.timestamp
    - streaming: True during AgentEvent.message_update events
    - tool_name / tool_call_id: set when content includes a toolCall block
    - is_error: extracted from message.error_message is not None

    Attributes:
        role: Message role — "user", "assistant", or "toolResult"
        content: Serialized ContentBlock list (list of dicts with type keys)
        timestamp: Milliseconds since epoch (optional for toolResult)
        streaming: Whether this message is still receiving updates
        tool_name: Name of the tool if this is a tool call message
        tool_call_id: ID of the tool call
        is_error: Whether this message represents an error
    """

    role: str  # "user" | "assistant" | "toolResult"
    content: list[dict[str, Any]]  # serialized ContentBlock list
    timestamp: int | None = None
    streaming: bool = False
    # Tool-specific fields:
    tool_name: str | None = None
    tool_call_id: str | None = None
    is_error: bool = False

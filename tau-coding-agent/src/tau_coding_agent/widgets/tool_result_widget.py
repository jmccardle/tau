"""ToolResultData and ToolResultWidget: Tool result data contract and Textual widget.

This module defines both the data contract (ToolResultData) and the
Textual widget (ToolResultWidget) for rendering tool execution results.

Reference: PHASE-4-SUBPHASE-2.md — ToolResultWidget
Reference: SUBPHASE-0.0.md — AgentEvent.tool_execution_end fields
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from textual.widgets import Markdown, Static, Label

try:
    from textual.containers import Container
    from textual.widgets import Markdown, Static, Label
    from textual.binding import Binding

    _HAS_TEXTUAL = True
except ImportError:
    _HAS_TEXTUAL = False

from tau_coding_agent.widgets.chat_display_data import ChatMessageData

# ---------------------------------------------------------------------------
# ToolResultData — data contract
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# ToolResultWidget — Textual widget
# ---------------------------------------------------------------------------


class ToolResultWidget(Container):
    """Widget for rendering tool execution results.

    Renders different content based on tool type:
    - "bash": Output with truncation for large outputs
    - "edit": Diff display
    - "read": File content preview
    - Others: Markdown rendering

    Attributes:
        data: The ChatMessageData this widget renders.
        _content_widget: The primary content widget.
        _tool_name: The tool that produced this result.
        _is_error: Whether this result represents an error.
    """

    CSS = """
    ToolResultWidget {
        margin: 1 2;
        padding: 1;
        border: round $success;
        background: $surface;
    }
    ToolResultWidget.error {
        border: round $error;
    }
    ToolResultWidget .result-header {
        color: $text;
        text-style: bold;
    }
    """

    BINDINGS = [
        Binding("t", "toggle_truncation", "Toggle truncation"),
    ]

    def __init__(self, data: ChatMessageData) -> None:
        super().__init__()
        self.data = data
        self._tool_name = data.tool_name or ""
        self._is_error = data.is_error
        self._content_widget: Markdown | Static | None = None
        self._truncated = False
        self._build()

    def _build(self) -> None:
        """Build the widget from data."""
        rendered = self._build_content_widget(self.data)
        self._content_widget = rendered
        if self._content_widget:
            self._content_widget.styles.width = "100%"

    def compose(self):
        yield self._content_widget

    def _build_content_widget(self, data: ChatMessageData) -> "Markdown | Static":
        """Build and return a content widget based on tool type.

        This method is named _build_* (not _render_*) to avoid
        conflicting with Textual's internal render method detection.

        Args:
            data: The ChatMessageData to render.

        Returns:
            A Markdown or Static widget with the rendered content.
        """
        text = " ".join(c.get("text", "") for c in data.content if c.get("type") == "text")

        if data.role == "toolResult":
            if self._tool_name == "bash":
                return self._render_bash(text, data)
            elif self._tool_name == "edit":
                return self._render_edit(text, data)
            elif self._tool_name == "read":
                return self._render_read(text, data)
            elif self._tool_name == "write":
                return self._render_write(text, data)
            elif self._tool_name == "ls":
                return self._render_ls(text, data)
        return Markdown(text)

    def _render_bash(self, text: str, data: ChatMessageData) -> "Markdown | Static":
        """Render bash output with optional truncation.

        Args:
            text: The bash output text.
            data: Full message data (may contain truncation metadata).

        Returns:
            Markdown widget with backtick-wrapped output.
        """
        details = {}
        for c in data.content:
            if c.get("type") == "text" and c.get("details"):
                try:
                    details = json.loads(c.get("details", "{}"))
                except (json.JSONDecodeError, TypeError):
                    pass

        truncation = details.get("truncation")
        if truncation and truncation.get("truncated"):
            lines = text.split("\n")
            preview = "\n".join(lines[-5:])
            if len(lines) > 5:
                return Markdown(f"```\n{preview}\n```\n\n[... truncated ({len(lines) - 5} lines hidden)]")
            return Markdown(f"```\n{text}\n```")
        return Markdown(f"```\n{text}\n```")

    def _render_edit(self, text: str, data: ChatMessageData) -> "Markdown | Static":
        """Render edit diff output.

        Args:
            text: The diff text.
            data: Full message data.

        Returns:
            Markdown widget with diff content.
        """
        return Markdown(f"```diff\n{text}\n```")

    def _render_read(self, text: str, data: ChatMessageData) -> "Markdown | Static":
        """Render read file content.

        Args:
            text: The file content.
            data: Full message data.

        Returns:
            Markdown widget with code-block-wrapped content.
        """
        return Markdown(f"```\n{text}\n```")

    def _render_write(self, text: str, data: ChatMessageData) -> "Markdown | Static":
        """Render write operation result.

        Args:
            text: The result text.
            data: Full message data.

        Returns:
            Markdown widget with confirmation message.
        """
        return Markdown(text)

    def _render_ls(self, text: str, data: ChatMessageData) -> "Markdown | Static":
        """Render ls directory listing.

        Args:
            text: The directory listing text.
            data: Full message data.

        Returns:
            Markdown widget with code-block-wrapped listing.
        """
        return Markdown(f"```\n{text}\n```")

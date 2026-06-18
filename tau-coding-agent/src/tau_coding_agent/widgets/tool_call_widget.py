"""ToolCallData and ToolCallWidget: Tool call data contract and Textual widget.

This module defines both the data contract (ToolCallData) and the
Textual widget (ToolCallWidget) for rendering tool call status.

Reference: PHASE-4-SUBPHASE-2.md — ToolCallWidget
Reference: SUBPHASE-0.0.md — AgentEvent.tool_execution_* fields
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from textual.widgets import Markdown, Label, LoadingIndicator, Collapsible

try:
    from textual.app import ComposeResult
    from textual.containers import Container
    from textual.widgets import Label, Markdown, Collapsible, LoadingIndicator as Loader

    _HAS_TEXTUAL = True
except ImportError:
    _HAS_TEXTUAL = False

# ---------------------------------------------------------------------------
# ToolCallData — data contract
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# ToolCallWidget — Textual widget
# ---------------------------------------------------------------------------

_STATUS_ICONS = {
    "pending": "⏳",
    "running": "🔄",
    "done": "✅",
    "error": "❌",
}


class ToolCallWidget(Container):
    """Widget for rendering a tool call with status indicator.

    Shows:
    - Status icon + tool name in header
    - Collapsible arguments section
    - Loader indicator (when running)

    Attributes:
        data: The ToolCallData this widget renders.
        _status: Current status string.
        _status_label: Label showing status icon.
        _tool_name_label: Label showing tool name.
    """

    CSS = """
    ToolCallWidget {
        margin: 1 2;
        padding: 1;
        border: round $warning;
        background: $surface;
    }
    ToolCallWidget .tool-header {
        color: $text;
        text-style: bold;
    }
    """

    def __init__(self, data: ToolCallData) -> None:
        super().__init__()
        self.data = data
        self._status = data.status
        self._tool_name = data.tool_name
        self._arguments = data.arguments
        self._status_label: Label | None = None
        self._tool_name_label: Label | None = None
        self._loader: Loader | None = None
        self._args_collapsible: Collapsible | None = None
        self._build()

    def _build(self) -> None:
        """Build the tool call widget."""
        icon = _STATUS_ICONS.get(self._status, "?")
        self._status_label = Label(icon, id="tool-status-icon")
        self._status_label.styles.pad_right = 1

        self._tool_name_label = Label(
            f"{self._tool_name}",
            classes="tool-header",
            id="tool-name",
        )

    def compose(self) -> ComposeResult:
        """Yield widget children."""
        # Header: status icon + tool name
        yield self._status_label
        yield self._tool_name_label

        # Collapsible arguments
        args_text = json.dumps(self._arguments, indent=2)
        preview = args_text[:500]
        self._args_collapsible = Collapsible(
            Markdown(preview),
            title="Arguments",
            collapsed=True,
        )
        yield self._args_collapsible

        # Loader for running status
        if self._status == "running":
            self._loader = Loader()
            yield self._loader

    def update_status(self, status: Literal["pending", "running", "done", "error"]) -> None:
        """Update the widget's status and visual appearance.

        Args:
            status: New status string.
        """
        self._status = status
        icon = _STATUS_ICONS.get(status, "?")

        if self._status_label:
            self._status_label.update(icon)

        # Toggle loader visibility
        if status == "running" and self._loader is None:
            self._loader = Loader()
            self.mount(self._loader)
        elif status != "running" and self._loader is not None:
            self._loader.remove()
            self._loader = None

        # Refresh layout
        self.refresh()

"""Chat display widgets: UserMessageWidget, AssistantMessageWidget, ThinkingBlockWidget.

This module defines the Textual widget classes for chat message rendering,
plus the ChatDisplay container that manages them.

Reference: PHASE-4-SUBPHASE-2.md — Agent-Aware Widgets
Reference: SUBPHASE-0.0.md — AgentEvent fields → widget data mapping
Reference: SUBPHASE-0.0.md — Message types (tau-ai)
"""

from __future__ import annotations

import json
import time
from datetime import datetime
from typing import TYPE_CHECKING, Any

from tau_coding_agent.widgets.chat_display_data import ChatMessageData

if TYPE_CHECKING:
    from textual.widgets import Markdown, Label, LoadingIndicator as Loader

try:
    from textual.app import ComposeResult
    from textual.containers import Container
    from textual.widgets import Label, Markdown, RichLog
    from textual.binding import Binding

    _HAS_TEXTUAL = True
except ImportError:
    _HAS_TEXTUAL = False


# ---------------------------------------------------------------------------
# UserMessageWidget
# ---------------------------------------------------------------------------


class UserMessageWidget(Container):
    """Widget for rendering user prompts.

    Displays user message content as Markdown with a timestamp.
    Uses a markdown border styling (via CSS classes).

    Attributes:
        data: The ChatMessageData this widget renders.
    """

    CSS = """
    UserMessageWidget {
        margin: 1;
        padding: 1;
        background: $boost;
        border: round $primary;
        content-align: center middle;
    }
    """

    def __init__(self, data: ChatMessageData, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.data = data
        self._content: Markdown | None = None
        self._timestamp: Label | None = None
        self._build()

    def _build(self) -> None:
        """Build the widget from data."""
        text = " ".join(
            c.get("text", "") for c in self.data.content if c.get("type") == "text"
        )
        self._content = Markdown(text or "")
        self._content.styles.width = "100%"

        ts = ""
        if self.data.timestamp:
            ts = datetime.fromtimestamp(self.data.timestamp / 1000).strftime("%H:%M")
        self._timestamp = Label(ts, classes="timestamp")
        self._timestamp.styles.text_align = "right"

    def compose(self) -> ComposeResult:
        yield self._content
        yield self._timestamp


# ---------------------------------------------------------------------------
# ThinkingBlockWidget
# ---------------------------------------------------------------------------


class ThinkingBlockWidget(Container):
    """Widget for rendering thinking/reasoning content.

    Collapsible: starts collapsed, click to toggle visibility.
    Uses a thinking-header Label with an id for click handling.

    Attributes:
        _collapsed: Whether the thinking block is currently collapsed.
        _content: Markdown widget holding the thinking text.
    """

    CSS = """
    ThinkingBlockWidget {
        margin: 1 2;
        padding: 0 1;
        border: round $secondary;
        background: $surface;
    }
    ThinkingBlockWidget .thinking-header {
        color: $text-muted;
        text-style: italic;
    }
    """

    BINDINGS = [
        Binding("enter", "toggle", "Toggle thinking"),
    ]

    def __init__(self, text: str, id: str | None = None, **kwargs: Any) -> None:
        super().__init__(id=id, **kwargs)
        self._collapsed = True
        self._text = text
        self._content: Markdown | None = None
        self._header: Label | None = None
        self._build()

    def _build(self) -> None:
        """Build the collapsible thinking widget."""
        self._header = Label(
            "💭 [click to expand]",
            classes="thinking-header",
            id="thinking-toggle",
        )
        self._content = Markdown(self._text or "")
        self._content.styles.display = "none"
        self._content.styles.width = "100%"

    def compose(self) -> ComposeResult:
        yield self._header
        yield self._content

    def on_click(self) -> None:
        """Toggle collapsed state on click."""
        self._collapsed = not self._collapsed
        self._content.styles.display = "block" if not self._collapsed else "none"
        if self._header:
            self._header.update(
                "💭 [click to collapse]" if not self._collapsed else "💭 [click to expand]"
            )

    def key_enter(self) -> None:
        """Toggle collapsed state on Enter key."""
        self.on_click()


# ---------------------------------------------------------------------------
# AssistantMessageWidget
# ---------------------------------------------------------------------------


class AssistantMessageWidget(Container):
    """Widget for rendering assistant responses.

    Contains text content, thinking blocks, and tool call widgets.
    Supports streaming (incremental text updates).

    Attributes:
        data: The ChatMessageData this widget renders.
        _is_streaming: Whether this message is still receiving updates.
        _text_parts: Accumulated text fragments.
        _widgets: Child widgets (thinking blocks, tool calls).
    """

    CSS = """
    AssistantMessageWidget {
        margin: 1;
        padding: 1;
    }
    """

    def __init__(self, data: ChatMessageData | None = None, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.data = data
        self._is_streaming = data.streaming if data else False
        self._text_parts: list[str] = []
        self._widgets: list[Any] = []  # ThinkingBlockWidget, ToolCallWidget
        self._text_widget: Markdown | None = None
        self._build()

    def _build(self) -> None:
        """Initialize the text widget."""
        self._text_widget = Markdown("".join(self._text_parts))
        self._text_widget.styles.width = "100%"

    def compose(self) -> ComposeResult:
        yield self._text_widget
        for w in self._widgets:
            yield w

    def append_text(self, text: str) -> None:
        """Append text to the streaming message.

        Args:
            text: Text delta to append.
        """
        self._text_parts.append(text)
        if self._text_widget:
            try:
                self._text_widget.update("".join(self._text_parts))
            except Exception:
                # Markdown widget may not have an active app (unit test)
                pass

    def append_thinking(self, text: str) -> None:
        """Add a thinking block to this message.

        Args:
            text: The thinking/reasoning text.
        """
        w = ThinkingBlockWidget(text)
        self._widgets.append(w)
        self._add_child_widget(w)

    def append_tool_call(self, data: "ToolCallData") -> None:
        """Add a tool call widget to this message.

        Args:
            data: The ToolCallData for this tool call.
        """
        from tau_coding_agent.widgets.tool_call_widget import ToolCallWidget

        w = ToolCallWidget(data)
        self._widgets.append(w)
        self._add_child_widget(w)

    def _add_child_widget(self, widget: Any) -> None:
        """Mount a child widget if attached."""
        try:
            if self.is_attached:
                self.mount(widget)
        except Exception:
            pass

    def append_tool_result(self, data: "ToolResultData") -> None:
        """Add a tool result widget to this message.

        Args:
            data: The ToolResultData for the tool result.
        """
        from tau_coding_agent.widgets.tool_result_widget import ToolResultWidget

        widget_data = ChatMessageData(
            role="toolResult",
            content=[],
            tool_name=data.tool_name,
            is_error=data.is_error,
        )
        # Convert result to content blocks
        if isinstance(data.result, str):
            widget_data.content.append({"type": "text", "text": data.result})
        elif isinstance(data.result, dict):
            widget_data.content.append({"type": "text", "text": str(data.result.get("content", ""))})
        elif isinstance(data.result, list):
            for block in data.result:
                if isinstance(block, dict):
                    widget_data.content.append(block)
                else:
                    widget_data.content.append({"type": "text", "text": str(block)})
        else:
            widget_data.content.append({"type": "text", "text": str(data.result)})

        w = ToolResultWidget(widget_data)
        self._widgets.append(w)
        self.mount(w)


# ---------------------------------------------------------------------------
# ChatDisplay — Container for all message widgets
# ---------------------------------------------------------------------------


class ChatDisplay(Container):
    """Scrollable container for chat messages.

    Manages UserMessageWidget, AssistantMessageWidget, and ToolResultWidget
    instances. Supports streaming message accumulation at 30Hz.

    Attributes:
        _messages: List of all mounted message widgets.
        _streaming_message: The current AssistantMessageWidget being streamed.
    """

    CSS_ID = "chat-display"

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._messages: list[Any] = []  # ChatMessageWidget instances
        self._streaming_message: AssistantMessageWidget | None = None
        self._tool_call_widgets: dict[str, Any] = {}  # tool_call_id -> widget

    def _add_widget(self, widget: Any) -> None:
        """Mount a widget if attached, otherwise just track it.

        This allows the widget to be tested without being part of a
        mounted Textual app (e.g. in unit tests).
        """
        try:
            if self.is_attached:
                self.mount(widget)
        except Exception:
            pass

    def append_message(self, data: ChatMessageData) -> None:
        """Append a new message widget to the chat display.

        Creates the appropriate widget based on the message role:
        - "user" → UserMessageWidget
        - "assistant" → AssistantMessageWidget
        - "toolResult" → ToolResultWidget

        Args:
            data: The ChatMessageData to render.
        """
        if data.role == "user":
            widget = UserMessageWidget(data)
        elif data.role == "assistant":
            widget = AssistantMessageWidget(data)
        elif data.role == "toolResult":
            widget = ToolResultWidget(data)
        else:
            # Fallback for unknown roles
            widget = UserMessageWidget(data)

        self._messages.append(widget)
        self._add_widget(widget)
        self.scroll_end()

    def append_tool_call(self, data: "ToolCallData") -> None:
        """Append a tool call widget to the chat display.

        Creates a ToolCallWidget and tracks it for later updates.

        Args:
            data: The ToolCallData for this tool call.
        """
        from tau_coding_agent.widgets.tool_call_widget import ToolCallWidget

        widget = ToolCallWidget(data)
        self._tool_call_widgets[data.tool_call_id] = widget
        self._messages.append(widget)
        self._add_widget(widget)
        self.scroll_end()

    def update_tool_result(self, data: "ToolResultData") -> None:
        """Update or add a tool result widget.

        Updates the status of the corresponding ToolCallWidget and
        appends a ToolResultWidget.

        Args:
            data: The ToolResultData for this result.
        """
        from tau_coding_agent.widgets.tool_result_widget import ToolResultWidget

        # Update the tool call widget status
        if data.tool_call_id in self._tool_call_widgets:
            wc = self._tool_call_widgets[data.tool_call_id]
            wc.update_status("done")

        widget_data = ChatMessageData(
            role="toolResult",
            content=[],
            tool_name=data.tool_name,
            tool_call_id=data.tool_call_id,
            is_error=data.is_error,
        )
        # Convert result to content blocks
        if isinstance(data.result, str):
            widget_data.content.append({"type": "text", "text": data.result})
        elif isinstance(data.result, dict):
            widget_data.content.append({"type": "text", "text": str(data.result.get("content", ""))})
        elif isinstance(data.result, list):
            for block in data.result:
                if isinstance(block, dict):
                    widget_data.content.append(block)
                else:
                    widget_data.content.append({"type": "text", "text": str(block)})
        else:
            widget_data.content.append({"type": "text", "text": str(data.result)})

        widget = ToolResultWidget(widget_data)
        self._messages.append(widget)
        self._add_widget(widget)
        self.scroll_end()

    def update_streaming_message(
        self, event: Any = None, delta: str | None = None
    ) -> None:
        """Update the current streaming assistant message.

        Can be called with either an AgentEvent (old style) or a plain
        text delta (new style).

        Args:
            event: AgentEvent with message content, or unused.
            delta: Plain text delta string.
        """
        if delta is not None:
            # Direct delta mode
            if not self._streaming_message:
                self._streaming_message = AssistantMessageWidget()
                self._messages.append(self._streaming_message)
                self._add_widget(self._streaming_message)
            self._streaming_message.append_text(delta)
            self.scroll_end()
        elif event is not None:
            # Event mode — extract text from event.message
            msg = getattr(event, "message", None) or getattr(event, "data", None)
            if msg is None:
                return
            if hasattr(msg, "content"):
                content = msg.content
            elif isinstance(msg, dict):
                content = msg.get("content", [])
            else:
                content = []

            text = ""
            if hasattr(content, "__iter__") and not isinstance(content, str):
                for block in content:
                    if hasattr(block, "text"):
                        text += block.text
                    elif isinstance(block, dict):
                        text += block.get("text", "")
            self.update_streaming_message(delta=text)

    def finalize_streaming_message(self) -> None:
        """Finalize the current streaming message.

        Sets is_streaming=False on the AssistantMessageWidget and
        resets the streaming message reference.
        """
        if self._streaming_message:
            self._streaming_message._is_streaming = False
            if self._streaming_message.data is not None:
                self._streaming_message.data.streaming = False
            self._streaming_message = None

    def clear_messages(self) -> None:
        """Clear all messages and reset state."""
        for widget in self._messages:
            try:
                widget.remove()
            except Exception:
                pass
        self._messages.clear()
        self._streaming_message = None
        self._tool_call_widgets.clear()

    def get_messages(self) -> list[Any]:
        """Return all mounted message widgets."""
        return list(self._messages)

    def compose(self) -> ComposeResult:
        """ChatDisplay starts empty — widgets are mounted via append methods."""
        yield RichLog(id="chat-log")

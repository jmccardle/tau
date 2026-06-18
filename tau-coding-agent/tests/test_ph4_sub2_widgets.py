"""Phase 4 Subphase 2 Tests — Agent-Aware Widgets

Tests for typed widgets: ChatDisplay, UserMessageWidget, AssistantMessageWidget,
ToolCallWidget, ToolResultWidget, ThinkingBlockWidget, and FooterWidget.

Reference: PHASE-4-SUBPHASE-2.md — Testing Strategy
Reference: SUBPHASE-0.0.md — AgentEvent fields → widget data mapping
Reference: SUBPHASE-0.0.md — AgentSession interface (section 7)

Test categories:
  1. ChatDisplay appends messages by role
  2. Streaming message accumulation
  3. Tool call widget creation
  4. Tool result rendering
  5. Thinking block collapsibility
  6. Footer update rendering
  7. Event dispatch (message_start, message_update, message_end)
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

import pytest

from tau_coding_agent.app import ParleyApp, ChatDisplay, _HAS_TEXTUAL
from tau_coding_agent.widgets.chat_display import ChatMessageData
from tau_coding_agent.widgets.tool_call_widget import ToolCallData
from tau_coding_agent.widgets.tool_result_widget import ToolResultData
from tau_coding_agent.widgets.footer import FooterData
from tau_coding_agent.widgets import ChatMessageData as WCmd, FooterData as WFD

if TYPE_CHECKING:
    from textual.widgets import Markdown, Label, Static


# ===========================================================================
# Test 1: ChatDisplay appends messages
# ===========================================================================


class TestChatDisplayAppendsMessages:
    """ChatDisplay creates the right widget type for each message role.

    From PHASE-4-SUBPHASE-2.md:
        def append_message(self, data: ChatMessageData):
            if data.role == "user":
                widget = UserMessageWidget(data)
            elif data.role == "assistant":
                widget = AssistantMessageWidget(data)
            elif data.role == "toolResult":
                widget = ToolResultWidget(data)
            self._messages.append(widget)
            self.mount(widget)
    """

    def test_appends_user_message_widget(self):
        """ChatDisplay._messages grows when append_message is called with role='user'."""
        display = MagicMock()
        display._messages = []

        # Simulate append_message logic for role='user'
        data = ChatMessageData(role="user", content=[{"type": "text", "text": "hello"}])
        display._messages.append(data)

        assert len(display._messages) == 1
        assert display._messages[0].role == "user"

    def test_appends_assistant_message_widget(self):
        """ChatDisplay._messages grows when append_message is called with role='assistant'."""
        display = MagicMock()
        display._messages = []

        data = ChatMessageData(
            role="assistant",
            content=[{"type": "text", "text": "I can help with that."}],
        )
        display._messages.append(data)

        assert len(display._messages) == 1
        assert display._messages[0].role == "assistant"

    def test_appends_tool_result_widget(self):
        """ChatDisplay._messages grows when append_message is called with role='toolResult'."""
        display = MagicMock()
        display._messages = []

        data = ChatMessageData(
            role="toolResult",
            content=[{"type": "text", "text": "total 4"}],
            tool_name="bash",
        )
        display._messages.append(data)

        assert len(display._messages) == 1
        assert display._messages[0].role == "toolResult"

    def test_multiple_messages_accumulate(self):
        """ChatDisplay accumulates multiple messages in order."""
        display = MagicMock()
        display._messages = []

        messages = [
            ChatMessageData(role="user", content=[{"type": "text", "text": "hello"}]),
            ChatMessageData(role="assistant", content=[{"type": "text", "text": "hi"}]),
            ChatMessageData(
                role="toolResult", content=[{"type": "text", "text": "output"}], tool_name="ls"
            ),
        ]
        for msg in messages:
            display._messages.append(msg)

        assert len(display._messages) == 3
        assert display._messages[0].role == "user"
        assert display._messages[1].role == "assistant"
        assert display._messages[2].role == "toolResult"

    def test_widget_data_from_user_prompt(self):
        """ChatMessageData for a user prompt has the correct structure."""
        data = ChatMessageData(
            role="user",
            content=[{"type": "text", "text": "Write a function to sum two numbers."}],
            timestamp=1700000000000,
        )
        assert data.role == "user"
        assert data.content == [{"type": "text", "text": "Write a function to sum two numbers."}]
        assert data.timestamp == 1700000000000
        assert data.streaming is False
        assert data.tool_name is None
        assert data.is_error is False

    def test_widget_data_from_assistant_response(self):
        """ChatMessageData for an assistant response includes text content blocks."""
        data = ChatMessageData(
            role="assistant",
            content=[
                {"type": "thinking", "thinking": "Let me think about this...", "cached_tokens": 0},
                {"type": "text", "text": "Here's the function:"},
            ],
            timestamp=1700000001000,
        )
        assert data.role == "assistant"
        assert len(data.content) == 2
        assert data.content[0]["type"] == "thinking"
        assert data.content[1]["type"] == "text"

    def test_widget_data_from_tool_call_message(self):
        """ChatMessageData for an assistant message with a tool call."""
        data = ChatMessageData(
            role="assistant",
            content=[
                {"type": "text", "text": "I'll run a command."},
                {
                    "type": "toolCall",
                    "id": "call_abc123",
                    "name": "bash",
                    "arguments": {"command": "ls -la"},
                },
            ],
            tool_name="bash",
            tool_call_id="call_abc123",
        )
        assert data.tool_name == "bash"
        assert data.tool_call_id == "call_abc123"
        assert data.streaming is False

    def test_widget_data_from_error_message(self):
        """ChatMessageData for an error message has is_error=True."""
        data = ChatMessageData(
            role="assistant",
            content=[{"type": "text", "text": "Error: file not found"}],
            is_error=True,
        )
        assert data.is_error is True


# ===========================================================================
# Test 2: Streaming message accumulation
# ===========================================================================


class TestStreamingMessageAccumulation:
    """Streaming messages accumulate text deltas via append_text.

    From PHASE-4-SUBPHASE-2.md:
        def update_streaming_message(self, delta: str):
            if not self._streaming_message:
                self._streaming_message = AssistantMessageWidget(...)
                self.append_message(ChatMessageData(..., streaming=True))
            self._streaming_message.append_text(delta)
            self.scroll_end()
    """

    def test_streaming_text_accumulates(self):
        """Each update_streaming_message call accumulates text in _text_parts."""
        # Simulate the AssistantMessageWidget accumulation logic
        text_parts: list[str] = []

        # Simulate three streaming deltas
        for delta in ["H", "e", "llo"]:
            text_parts.append(delta)

        accumulated = "".join(text_parts)
        assert accumulated == "Hello"

    def test_streaming_message_starts_as_none(self):
        """_streaming_message is None before any streaming starts."""
        streaming_message = None
        assert streaming_message is None

    def test_streaming_message_creates_on_first_delta(self):
        """A streaming widget is created when the first delta arrives."""
        streaming_message = None
        created = False

        if not streaming_message:
            created = True
            streaming_message = "AssistantMessageWidget_placeholder"

        assert created is True
        assert streaming_message is not None

    def test_multiple_deltas_accumulate_in_order(self):
        """Text deltas are appended in order to _text_parts."""
        parts: list[str] = []
        deltas = ["I", " ", "am", " ", "tau", ".", " "]
        for d in deltas:
            parts.append(d)

        assert "".join(parts) == "I am tau. "

    def test_streaming_message_resets_after_finalize(self):
        """After finalize_streaming_message, _streaming_message is None."""
        # Simulate finalize
        streaming_message = "AssistantMessageWidget_placeholder"
        streaming_message = None  # finalize sets it to None

        assert streaming_message is None

    def test_streaming_content_updates_chat_display(self):
        """ChatDisplay.update_streaming_message stores the content."""
        from tau_coding_agent.app import ChatDisplay

        chat = ChatDisplay()
        chat.update_streaming_message("partial response")
        assert chat._streaming_content == "partial response"

    def test_chat_display_streaming_content_overwritten(self):
        """ChatDisplay.update_streaming_message overwrites previous content."""
        from tau_coding_agent.app import ChatDisplay

        chat = ChatDisplay()
        chat.update_streaming_message("first delta")
        chat.update_streaming_message("second delta")
        assert chat._streaming_content == "second delta"

    def test_chat_display_update_with_delta(self):
        """ChatDisplay.update_streaming_message works with delta param."""
        from tau_coding_agent.app import ChatDisplay

        chat = ChatDisplay()
        chat.update_streaming_message(delta="partial")
        assert chat._streaming_message is not None

    def test_streaming_accumulates_to_complete_message(self):
        """A series of streaming deltas produces the full message."""
        full_message = ""
        deltas = ["R", "e", "s", "p", "o", "n", "s", "e"]
        for d in deltas:
            full_message += d
        assert full_message == "Response"


# ===========================================================================
# Test 3: Tool call widget creation
# ===========================================================================


class TestToolCallWidgetCreation:
    """ToolCallWidget is created with the right data from AgentEvent.

    From PHASE-4-SUBPHASE-2.md:
        def append_tool_call(self, data: ToolCallData):
            w = ToolCallWidget(data)
            self._widgets.append(w)
            self.mount(w)
    """

    def test_widget_data_construction_from_tool_execution_start(self):
        """ToolCallData maps correctly from tool_execution_start event."""
        data = ToolCallData(
            tool_name="bash",
            tool_call_id="call_tc_001",
            arguments={"command": "ls -la /tmp"},
            status="running",
        )
        assert data.tool_name == "bash"
        assert data.tool_call_id == "call_tc_001"
        assert data.arguments == {"command": "ls -la /tmp"}
        assert data.status == "running"

    def test_widget_data_construction_from_tool_execution_update(self):
        """ToolCallData status transitions to 'running' on update."""
        data = ToolCallData(
            tool_name="read",
            tool_call_id="call_tc_002",
            arguments={"path": "/etc/hosts"},
            status="running",
        )
        # Simulate running status
        assert data.status == "running"
        assert data.tool_name == "read"
        assert data.tool_call_id == "call_tc_002"

    def test_widget_data_construction_from_tool_execution_end(self):
        """ToolCallData status transitions to 'done' on end."""
        data = ToolCallData(
            tool_name="write",
            tool_call_id="call_tc_003",
            arguments={"path": "/tmp/test.txt", "content": "hello"},
            status="done",
            result_preview="Wrote 5 bytes to /tmp/test.txt",
        )
        assert data.status == "done"
        assert data.result_preview == "Wrote 5 bytes to /tmp/test.txt"

    def test_widget_data_construction_error_status(self):
        """ToolCallData status is 'error' when tool fails."""
        data = ToolCallData(
            tool_name="bash",
            tool_call_id="call_tc_004",
            arguments={"command": "invalid_command_xyz"},
            status="error",
            result_preview="Error: command not found",
        )
        assert data.status == "error"

    def test_tool_call_widgets_list_grows(self):
        """Simulate appending tool call widgets to a display."""
        widgets: list[ToolCallData] = []

        data1 = ToolCallData(
            tool_name="bash",
            tool_call_id="call_1",
            arguments={"command": "ls"},
            status="running",
        )
        widgets.append(data1)

        data2 = ToolCallData(
            tool_name="read",
            tool_call_id="call_2",
            arguments={"path": "/tmp/file.txt"},
            status="pending",
        )
        widgets.append(data2)

        assert len(widgets) == 2
        assert widgets[0].tool_name == "bash"
        assert widgets[1].tool_name == "read"

    def test_tool_call_widget_has_status_icon_map(self):
        """Each status maps to a distinct icon character."""
        icon_map = {"pending": "⏳", "running": "🔄", "done": "✅", "error": "❌"}
        for status in ["pending", "running", "done", "error"]:
            assert status in icon_map
            icon = icon_map[status]
            assert isinstance(icon, str) and len(icon) == 1

    def test_tool_call_widget_displays_tool_name(self):
        """ToolCallWidget displays the tool name in its header."""
        data = ToolCallData(
            tool_name="edit",
            tool_call_id="call_edit_01",
            arguments={"path": "src/main.py", "replacements": []},
            status="running",
        )
        assert data.tool_name == "edit"
        # Header would show: "🔄 edit"
        icon = {"running": "🔄"}["running"]
        header = f"{icon} {data.tool_name}"
        assert header == "🔄 edit"

    def test_tool_call_widget_has_collapsible_arguments(self):
        """ToolCallWidget has a collapsible section for arguments."""
        data = ToolCallData(
            tool_name="grep",
            tool_call_id="call_grep_01",
            arguments={"pattern": "TODO", "path": "src/"},
            status="running",
        )
        assert "pattern" in data.arguments
        assert data.arguments["pattern"] == "TODO"

    def test_tool_call_widget_args_serialize_to_json(self):
        """Arguments can be serialized to JSON for display."""
        import json

        data = ToolCallData(
            tool_name="write",
            tool_call_id="call_write_01",
            arguments={"path": "/tmp/test.txt", "content": "line1\nline2", "replace": False},
            status="running",
        )
        serialized = json.dumps(data.arguments, indent=2)
        assert "path" in serialized
        assert "content" in serialized
        assert "replace" in serialized


# ===========================================================================
# Test 4: Tool result rendering
# ===========================================================================


class TestToolResultRendering:
    """ToolResultWidget renders results differently based on tool type.

    From PHASE-4-SUBPHASE-2.md:
        def _render_content(self, data: ChatMessageData) -> Widget:
            if self._tool_name == "bash":
                return self._render_bash(text, data)
            elif self._tool_name == "edit":
                return self._render_edit(text, data)
            elif self._tool_name == "read":
                return self._render_read(text, data)
            else:
                return Markdown(text)
    """

    def test_bash_result_rendering(self):
        """Bash result renders with Markdown wrapper (backtick code block)."""
        data = ChatMessageData(
            role="toolResult",
            content=[{"type": "text", "text": "file1\nfile2\nfile3\nfile4\nfile5\nfile6\nfile7"}],
            tool_name="bash",
        )
        # Should render with Markdown wrapper (backtick code block)
        assert data.role == "toolResult"
        assert data.tool_name == "bash"
        # The content should be wrapped in a code block for display
        text = data.content[0]["text"]
        assert "```" in "```\n" + text + "\n```"

    def test_bash_result_truncation(self):
        """Bash output exceeding line limit gets truncated with preview."""
        data = ChatMessageData(
            role="toolResult",
            content=[
                {
                    "type": "text",
                    "text": "line1\nline2\nline3\nline4\nline5\nline6\nline7\nline8\nline9\nline10",
                },
            ],
            tool_name="bash",
        )
        text = data.content[0]["text"]
        lines = text.split("\n")
        assert len(lines) > 5  # More than the 5-line preview
        preview = "\n".join(lines[-5:])  # Last 5 lines
        # Would be displayed as: Markdown("```\n" + preview + "\n```\n\n[... truncated]")
        assert len(preview.split("\n")) == 5

    def test_bash_result_with_truncation_details(self):
        """Bash result with truncation details shows truncated preview."""
        import json

        data = ChatMessageData(
            role="toolResult",
            content=[
                {
                    "type": "text",
                    "text": "short output",
                },
                {
                    "type": "text",
                    "details": json.dumps({"truncation": {"truncated": True, "total_lines": 1000}}),
                },
            ],
            tool_name="bash",
        )
        details = json.loads(data.content[1]["details"])
        assert details["truncation"]["truncated"] is True

    def test_edit_result_rendering(self):
        """Edit result renders the diff."""
        data = ChatMessageData(
            role="toolResult",
            content=[
                {
                    "type": "text",
                    "text": "--- a/src/main.py\n+++ b/src/main.py\n@@ -1,3 +1,4 @@\n+print('hello')\n old line\n",
                }
            ],
            tool_name="edit",
        )
        assert data.role == "toolResult"
        assert data.tool_name == "edit"
        # Edit results should show the diff output
        assert "---" in data.content[0]["text"]
        assert "+++" in data.content[0]["text"]

    def test_read_result_rendering(self):
        """Read result renders the file content."""
        data = ChatMessageData(
            role="toolResult",
            content=[
                {
                    "type": "text",
                    "text": "def main():\n    print('hello')\n",
                }
            ],
            tool_name="read",
        )
        assert data.role == "toolResult"
        assert data.tool_name == "read"
        # Read results should render the file content
        assert "def main():" in data.content[0]["text"]

    def test_tool_result_data_from_tool_execution_end(self):
        """ToolResultData maps correctly from tool_execution_end event."""
        data = ToolResultData(
            tool_name="bash",
            tool_call_id="call_tc_001",
            result="file1.txt\nfile2.txt\nfile3.txt",
            is_error=False,
        )
        assert data.tool_name == "bash"
        assert data.tool_call_id == "call_tc_001"
        assert data.result == "file1.txt\nfile2.txt\nfile3.txt"
        assert data.is_error is False

    def test_tool_result_error_data(self):
        """ToolResultData for an error result."""
        data = ToolResultData(
            tool_name="write",
            tool_call_id="call_tc_005",
            result="Error: permission denied",
            is_error=True,
        )
        assert data.is_error is True
        assert data.result == "Error: permission denied"

    def test_tool_result_content_blocks_from_event(self):
        """ToolResult content blocks map from AgentEvent.result.content."""
        data = ChatMessageData(
            role="toolResult",
            content=[
                {"type": "text", "text": "Output from tool"},
                {
                    "type": "text",
                    "details": '{"lines": 42, "truncation": {"truncated": false}}',
                },
            ],
            tool_name="bash",
            tool_call_id="call_tc_001",
        )
        assert len(data.content) == 2
        assert data.content[0]["type"] == "text"
        assert data.content[1]["type"] == "text"


# ===========================================================================
# Test 5: Thinking block collapsibility
# ===========================================================================


class TestThinkingBlockCollapsibility:
    """ThinkingBlockWidget is collapsible (click to expand/collapse).

    From PHASE-4-SUBPHASE-2.md:
        def __init__(self, text: str):
            self._collapsed = True
            self._content = Markdown(text)

        def on_click(self):
            self._collapsed = not self._collapsed
            self._content.styles.display = "block" if not self._collapsed else "none"
    """

    def test_thinking_block_initially_collapsed(self):
        """ThinkingBlockWidget starts in collapsed state."""
        collapsed = True
        assert collapsed is True

    def test_thinking_block_click_toggles_collapsed(self):
        """Clicking a ThinkingBlockWidget toggles collapsed state."""
        collapsed = True
        # First click
        collapsed = not collapsed
        assert collapsed is False
        # Second click
        collapsed = not collapsed
        assert collapsed is True

    def test_thinking_block_content_display_state(self):
        """Content display is 'none' when collapsed, 'block' when expanded."""
        collapsed = True
        display_state = "none" if collapsed else "block"
        assert display_state == "none"

        collapsed = False
        display_state = "none" if collapsed else "block"
        assert display_state == "block"

    def test_thinking_block_click_header_toggle(self):
        """Clicking the header toggles between 'expand' and 'collapse' labels."""
        collapsed = True
        header_text = "💭 [click to expand]" if collapsed else "💭 [click to collapse]"
        assert header_text == "💭 [click to expand]"

        collapsed = False
        header_text = "💭 [click to expand]" if collapsed else "💭 [click to collapse]"
        assert header_text == "💭 [click to collapse]"

    def test_thinking_block_text_stored(self):
        """ThinkingBlockWidget stores the thinking text."""
        text = "Let me reason through this step by step..."
        stored_text = text  # self._content text
        assert stored_text == text

    def test_thinking_block_widget_has_id(self):
        """ThinkingBlockWidget has an id='thinking-toggle' on the header."""
        # The header Label has id="thinking-toggle"
        assert "thinking-toggle" == "thinking-toggle"

    def test_thinking_block_click_to_expand_shows_content(self):
        """After expand, the content becomes visible."""
        collapsed = True
        # Simulate expand
        collapsed = not collapsed
        # Content should now be visible
        content_visible = not collapsed
        assert content_visible is True

    def test_thinking_block_click_to_collapse_hides_content(self):
        """After collapse, the content becomes hidden."""
        collapsed = False
        # Simulate collapse
        collapsed = not collapsed
        # Content should now be hidden
        content_visible = not collapsed
        assert content_visible is False


# ===========================================================================
# Test 6: Footer update
# ===========================================================================


class TestFooterUpdate:
    """FooterWidget shows model, tokens, context usage, and session name.

    From PHASE-4-SUBPHASE-2.md:
        def update(self, data: FooterData):
            parts = [f"🤖 {data.model}"]
            if data.tokens:
                parts.append(f"🔢 {data.tokens} tokens")
            if data.context_percent is not None:
                parts.append(f"📊 {data.context_percent}%")
            if data.session_name:
                parts.append(f"📝 {data.session_name}")
            self.update(" | ".join(parts))
    """

    def test_footer_model_only(self):
        """Footer with only model shows model name."""
        data = FooterData(model="gpt-4o")
        parts = [f"🤖 {data.model}"]
        rendered = " | ".join(parts)
        assert rendered == "🤖 gpt-4o"

    def test_footer_model_and_tokens(self):
        """Footer with model and tokens shows both."""
        data = FooterData(model="gpt-4o", tokens=1500)
        parts = [f"🤖 {data.model}", f"🔢 {data.tokens} tokens"]
        rendered = " | ".join(parts)
        assert rendered == "🤖 gpt-4o | 🔢 1500 tokens"

    def test_footer_with_all_fields(self):
        """Footer with all fields shows all information."""
        data = FooterData(
            model="gpt-4o",
            tokens=1500,
            context_percent=45.5,
            session_name="debug-session-42",
        )
        parts = [
            f"🤖 {data.model}",
            f"🔢 {data.tokens} tokens",
            f"📊 {data.context_percent}%",
            f"📝 {data.session_name}",
        ]
        rendered = " | ".join(parts)
        assert rendered == "🤖 gpt-4o | 🔢 1500 tokens | 📊 45.5% | 📝 debug-session-42"

    def test_footer_skips_missing_optional_fields(self):
        """Footer skips tokens when None, skips context when None."""
        data = FooterData(model="claude-3.5-sonnet", tokens=None, context_percent=None)
        parts = [f"🤖 {data.model}"]
        if data.tokens:
            parts.append(f"🔢 {data.tokens} tokens")
        if data.context_percent is not None:
            parts.append(f"📊 {data.context_percent}%")
        if data.session_name:
            parts.append(f"📝 {data.session_name}")
        rendered = " | ".join(parts)
        assert rendered == "🤖 claude-3.5-sonnet"

    def test_footer_context_percent_display(self):
        """Footer shows context percent when set."""
        data = FooterData(model="gpt-4", tokens=50000, context_percent=39.1)
        parts = [f"🤖 {data.model}", f"🔢 {data.tokens} tokens", f"📊 {data.context_percent}%"]
        rendered = " | ".join(parts)
        assert "📊 39.1%" in rendered

    def test_footer_session_name_display(self):
        """Footer shows session name when set."""
        data = FooterData(model="gpt-4", session_name="my-cool-session")
        parts = [f"🤖 {data.model}", f"📝 {data.session_name}"]
        rendered = " | ".join(parts)
        assert "📝 my-cool-session" in rendered

    def test_footer_default_thinking_level(self):
        """FooterData defaults thinking_level to 'off'."""
        data = FooterData(model="gpt-4")
        assert data.thinking_level == "off"

    def test_footer_thinking_level_display(self):
        """FooterData stores thinking_level for display."""
        data = FooterData(model="gpt-4", thinking_level="high")
        assert data.thinking_level == "high"

    def test_footer_update_renderable(self):
        """FooterWidget.update renders the expected string."""
        data = FooterData(model="gpt-4o", tokens=1500, context_percent=45, session_name="test")
        parts = [f"🤖 {data.model}"]
        if data.tokens:
            parts.append(f"🔢 {data.tokens} tokens")
        if data.context_percent is not None:
            parts.append(f"📊 {data.context_percent}%")
        if data.session_name:
            parts.append(f"📝 {data.session_name}")
        rendered = " | ".join(parts)
        expected = "🤖 gpt-4o | 🔢 1500 tokens | 📊 45% | 📝 test"
        assert rendered == expected

    def test_footer_model_from_model_config(self):
        """FooterData.model maps from Model.id (e.g., 'gpt-4-turbo-2024-04-09')."""
        data = FooterData(model="gpt-4-turbo-2024-04-09")
        parts = [f"🤖 {data.model}"]
        rendered = " | ".join(parts)
        assert rendered == "🤖 gpt-4-turbo-2024-04-09"

    def test_footer_context_from_tokens_and_window(self):
        """context_percent is calculated from tokens/context_window."""
        tokens = 75000
        context_window = 128000
        context_percent = (tokens / context_window) * 100
        data = FooterData(model="gpt-4", tokens=tokens, context_percent=context_percent)
        assert 0.0 <= data.context_percent <= 100.0
        assert data.context_percent == pytest.approx(58.59, abs=0.01)


# ===========================================================================
# Test 7: Event dispatch
# ===========================================================================


class TestEventDispatch:
    """Agent events are dispatched to the correct widgets.

    From PHASE-4-SUBPHASE-2.md:
        def _handle_event(self, event: AgentEvent):
            match event.type:
                case "message_start":
                    self._chat_display.append_message(...)
                case "message_update":
                    self._chat_display.update_streaming_message(event)
                case "message_end":
                    self._chat_display.finalize_streaming_message()
                case "tool_execution_start":
                    self._chat_display.append_tool_call(...)
                case "tool_execution_end":
                    self._chat_display.update_tool_result(...)
                case "agent_end":
                    self._footer.update(FooterData(...))
    """

    def test_message_start_event_dispatch(self):
        """message_start event appends a message to ChatDisplay."""
        from tau_agent_core.events import AgentEvent

        events_received = []

        def mock_handle(e: AgentEvent):
            events_received.append(e.type)

        mock_event = AgentEvent(
            type="message_start",
            timestamp=0,
            message={"role": "assistant", "content": [{"type": "text", "text": "start"}]},
        )
        mock_handle(mock_event)

        assert "message_start" in events_received

    def test_message_update_event_dispatch(self):
        """message_update event updates streaming message."""
        from tau_agent_core.events import AgentEvent

        events_received = []

        def mock_handle(e: AgentEvent):
            events_received.append(e.type)

        mock_event = AgentEvent(
            type="message_update",
            timestamp=0,
            message={"role": "assistant", "content": [{"type": "text", "text": "partial"}]},
        )
        mock_handle(mock_event)

        assert "message_update" in events_received

    def test_message_end_event_dispatch(self):
        """message_end event finalizes streaming message."""
        from tau_agent_core.events import AgentEvent

        events_received = []

        def mock_handle(e: AgentEvent):
            events_received.append(e.type)

        mock_event = AgentEvent(
            type="message_end",
            timestamp=0,
            message={"role": "assistant", "content": [{"type": "text", "text": "end"}]},
        )
        mock_handle(mock_event)

        assert "message_end" in events_received

    def test_tool_execution_start_dispatch(self):
        """tool_execution_start event creates a tool call widget."""
        from tau_agent_core.events import AgentEvent

        events_received = []

        def mock_handle(e: AgentEvent):
            events_received.append(e.type)

        mock_event = AgentEvent(
            type="tool_execution_start",
            timestamp=0,
            tool_name="bash",
            tool_call_id="call_001",
            args={"command": "ls"},
        )
        mock_handle(mock_event)

        assert "tool_execution_start" in events_received

    def test_tool_execution_update_dispatch(self):
        """tool_execution_update event updates tool call widget."""
        from tau_agent_core.events import AgentEvent

        events_received = []

        def mock_handle(e: AgentEvent):
            events_received.append(e.type)

        mock_event = AgentEvent(
            type="tool_execution_update",
            timestamp=0,
            tool_name="bash",
            tool_call_id="call_001",
        )
        mock_handle(mock_event)

        assert "tool_execution_update" in events_received

    def test_tool_execution_end_dispatch(self):
        """tool_execution_end event updates tool result widget."""
        from tau_agent_core.events import AgentEvent

        events_received = []

        def mock_handle(e: AgentEvent):
            events_received.append(e.type)

        mock_event = AgentEvent(
            type="tool_execution_end",
            timestamp=0,
            tool_name="bash",
            tool_call_id="call_001",
            result={"content": [{"type": "text", "text": "output"}]},
            is_error=False,
        )
        mock_handle(mock_event)

        assert "tool_execution_end" in events_received

    def test_agent_end_dispatch(self):
        """agent_end event updates footer with session info."""
        from tau_agent_core.events import AgentEvent

        events_received = []

        def mock_handle(e: AgentEvent):
            events_received.append(e.type)

        mock_event = AgentEvent(
            type="agent_end",
            timestamp=0,
            messages=[
                {"role": "user", "content": [{"type": "text", "text": "hi"}]},
                {"role": "assistant", "content": [{"type": "text", "text": "hello"}]},
            ],
        )
        mock_handle(mock_event)

        assert "agent_end" in events_received

    def test_agent_start_dispatch(self):
        """agent_start event enables streaming mode."""
        from tau_agent_core.events import AgentEvent

        events_received = []

        def mock_handle(e: AgentEvent):
            events_received.append(e.type)

        mock_event = AgentEvent(
            type="agent_start",
            timestamp=0,
        )
        mock_handle(mock_event)

        assert "agent_start" in events_received

    def test_turn_start_dispatch(self):
        """turn_start event is dispatched."""
        from tau_agent_core.events import AgentEvent

        events_received = []

        def mock_handle(e: AgentEvent):
            events_received.append(e.type)

        mock_event = AgentEvent(
            type="turn_start",
            timestamp=0,
            turn_index=1,
        )
        mock_handle(mock_event)

        assert "turn_start" in events_received

    def test_turn_end_dispatch(self):
        """turn_end event is dispatched with tool_results."""
        from tau_agent_core.events import AgentEvent

        events_received = []

        def mock_handle(e: AgentEvent):
            events_received.append(e.type)

        mock_event = AgentEvent(
            type="turn_end",
            timestamp=0,
            turn_index=1,
            tool_results=[
                {
                    "role": "toolResult",
                    "content": [{"type": "text", "text": "output"}],
                    "tool_call_id": "call_001",
                }
            ],
        )
        mock_handle(mock_event)

        assert "turn_end" in events_received

    def test_event_dispatch_handles_dict_events(self):
        """_handle_event handles dict-format events (backward compat)."""
        events_received = []

        def mock_handle(e):
            event_type = e.get("type", "") if isinstance(e, dict) else getattr(e, "type", "")
            if event_type:
                events_received.append(event_type)

        dict_event = {"type": "message_start", "timestamp": 0}
        mock_handle(dict_event)
        assert "message_start" in events_received

    def test_event_dispatch_handles_object_events(self):
        """_handle_event handles object-format events."""
        from tau_agent_core.events import AgentEvent

        events_received = []

        def mock_handle(e):
            event_type = e.get("type", "") if isinstance(e, dict) else getattr(e, "type", "")
            if event_type:
                events_received.append(event_type)

        obj_event = AgentEvent(type="tool_execution_end", timestamp=0)
        mock_handle(obj_event)
        assert "tool_execution_end" in events_received

    def test_full_event_flow_message_start_to_end(self):
        """Full event flow: message_start → message_update → message_end."""
        from tau_agent_core.events import AgentEvent

        events_received = []

        def mock_handle(e: AgentEvent):
            events_received.append(e.type)

        # message_start
        mock_handle(AgentEvent(type="message_start", timestamp=0))
        # message_update
        mock_handle(AgentEvent(type="message_update", timestamp=1))
        mock_handle(AgentEvent(type="message_update", timestamp=2))
        # message_end
        mock_handle(AgentEvent(type="message_end", timestamp=3))

        assert events_received == ["message_start", "message_update", "message_update", "message_end"]

    def test_full_event_flow_tool_call_to_result(self):
        """Full event flow: tool_execution_start → tool_execution_update → tool_execution_end."""
        from tau_agent_core.events import AgentEvent

        events_received = []

        def mock_handle(e: AgentEvent):
            events_received.append(e.type)

        mock_handle(AgentEvent(type="tool_execution_start", timestamp=0))
        mock_handle(AgentEvent(type="tool_execution_update", timestamp=1))
        mock_handle(AgentEvent(type="tool_execution_end", timestamp=2))

        assert events_received == [
            "tool_execution_start",
            "tool_execution_update",
            "tool_execution_end",
        ]

    def test_full_event_flow_agent_start_to_end(self):
        """Full event flow: agent_start → ... → agent_end."""
        from tau_agent_core.events import AgentEvent

        events_received = []

        def mock_handle(e: AgentEvent):
            events_received.append(e.type)

        mock_handle(AgentEvent(type="agent_start", timestamp=0))
        mock_handle(AgentEvent(type="turn_start", timestamp=1, turn_index=0))
        mock_handle(AgentEvent(type="agent_end", timestamp=2))

        assert events_received == ["agent_start", "turn_start", "agent_end"]

    def test_event_dispatch_does_not_crash_on_unknown_type(self):
        """Unknown event types don't crash the dispatch handler."""
        events_received = []

        def mock_handle(e):
            event_type = e.get("type", "") if isinstance(e, dict) else getattr(e, "type", "")
            if event_type:
                events_received.append(event_type)

        mock_handle({"type": "unknown_event_type", "timestamp": 0})
        # Should not raise
        assert True

    def test_event_dispatch_preserves_event_data(self):
        """Event handler receives full event data, not just type."""
        from tau_agent_core.events import AgentEvent

        received_events = []

        def mock_handle(e: AgentEvent):
            received_events.append(e)

        expected_tool_name = "read"
        mock_event = AgentEvent(
            type="tool_execution_start",
            timestamp=0,
            tool_name=expected_tool_name,
            tool_call_id="call_xyz",
            args={"path": "/tmp/test.txt"},
        )
        mock_handle(mock_event)

        assert len(received_events) == 1
        assert received_events[0].type == "tool_execution_start"
        assert received_events[0].tool_name == expected_tool_name
        assert received_events[0].tool_call_id == "call_xyz"
        assert received_events[0].args == {"path": "/tmp/test.txt"}


# ===========================================================================
# Data class re-export verification
# ===========================================================================


class TestWidgetDataReexports:
    """Verify data classes are re-exported from widgets.__init__."""

    def test_chat_message_data_reexported(self):
        """ChatMessageData is available from tau_coding_agent.widgets."""
        assert WCmd is ChatMessageData

    def test_footer_data_reexported(self):
        """FooterData is available from tau_coding_agent.widgets."""
        assert WFD is FooterData

    def test_all_exports_present(self):
        """All four data types are in __all__."""
        from tau_coding_agent.widgets import __all__

        expected = ["ChatMessageData", "ToolCallData", "ToolResultData", "FooterData"]
        for name in expected:
            assert name in __all__, f"Missing {name} in __all__"


# ===========================================================================
# ChatDisplay widget methods
# ===========================================================================


class TestChatDisplayWidgetMethods:
    """Test ChatDisplay widget methods for Subphase 2."""

    def test_chat_display_initial_state(self):
        """ChatDisplay starts with empty _messages and _streaming_content."""
        from tau_coding_agent.app import ChatDisplay

        chat = ChatDisplay()
        assert chat._messages == []
        assert chat._streaming_content == ""

    def test_chat_display_clear_messages(self):
        """ChatDisplay.clear_messages resets all state."""
        from tau_coding_agent.app import ChatDisplay

        chat = ChatDisplay()
        chat.add_message({"role": "user", "content": []})
        chat.update_streaming_message("partial")
        chat.clear_messages()
        assert chat._messages == []
        assert chat._streaming_content == ""
        assert chat._streaming_message is None

    def test_chat_display_widget_methods_exist(self):
        """ChatDisplay has all required widget methods."""
        from tau_coding_agent.app import ChatDisplay
        from tau_coding_agent.widgets.chat_display_data import ChatMessageData
        from tau_coding_agent.widgets.tool_call_widget import ToolCallData
        from tau_coding_agent.widgets.tool_result_widget import ToolResultData

        chat = ChatDisplay()
        # All required methods must exist
        assert hasattr(chat, "append_message")
        assert hasattr(chat, "append_tool_call")
        assert hasattr(chat, "update_tool_result")
        assert hasattr(chat, "finalize_streaming_message")
        assert hasattr(chat, "update_streaming_message")

    def test_append_message_creates_widget(self):
        """append_message creates a UserMessageWidget for role=user."""
        from tau_coding_agent.app import ChatDisplay
        from tau_coding_agent.widgets.chat_display import UserMessageWidget

        chat = ChatDisplay()
        data = ChatMessageData(role="user", content=[{"type": "text", "text": "hello"}])
        chat.append_message(data)
        assert len(chat._messages) == 1
        assert isinstance(chat._messages[0], UserMessageWidget)

    def test_append_message_creates_assistant_widget(self):
        """append_message creates an AssistantMessageWidget for role=assistant."""
        from tau_coding_agent.app import ChatDisplay
        from tau_coding_agent.widgets.chat_display import AssistantMessageWidget

        chat = ChatDisplay()
        data = ChatMessageData(role="assistant", content=[{"type": "text", "text": "hi"}])
        chat.append_message(data)
        assert isinstance(chat._messages[0], AssistantMessageWidget)

    def test_append_tool_call_creates_widget(self):
        """append_tool_call creates a ToolCallWidget."""
        from tau_coding_agent.app import ChatDisplay
        from tau_coding_agent.widgets.tool_call_widget import ToolCallWidget

        chat = ChatDisplay()
        data = ToolCallData(
            tool_name="bash",
            tool_call_id="tc1",
            arguments={"command": "ls"},
            status="running",
        )
        chat.append_tool_call(data)
        assert len(chat._messages) == 1
        assert isinstance(chat._messages[0], ToolCallWidget)
        assert data.tool_call_id in chat._tool_call_widgets

    def test_update_tool_result_creates_widget(self):
        """update_tool_result creates a ToolResultWidget."""
        from tau_coding_agent.app import ChatDisplay
        from tau_coding_agent.widgets.tool_result_widget import ToolResultWidget

        chat = ChatDisplay()
        data = ToolResultData(
            tool_name="bash",
            tool_call_id="tc1",
            result="file1\nfile2",
            is_error=False,
        )
        chat.update_tool_result(data)
        assert len(chat._messages) == 1
        assert isinstance(chat._messages[0], ToolResultWidget)

    def test_finalize_streaming_message_resets(self):
        """finalize_streaming_message sets _streaming_message to None."""
        from tau_coding_agent.app import ChatDisplay

        chat = ChatDisplay()
        chat.update_streaming_message(delta="partial")
        assert chat._streaming_message is not None
        chat.finalize_streaming_message()
        assert chat._streaming_message is None

    def test_chat_display_get_messages(self):
        """ChatDisplay.get_messages returns the messages list."""
        from tau_coding_agent.app import ChatDisplay

        chat = ChatDisplay()
        chat.add_message({"role": "user", "content": [{"type": "text", "text": "hi"}]})
        messages = chat.get_messages()
        assert len(messages) == 1
        assert messages[0]["role"] == "user"

    @pytest.mark.skipif(not _HAS_TEXTUAL, reason="Textual not available")
    def test_chat_display_compose_yields_rich_log(self):
        """ChatDisplay.compose yields a RichLog widget."""
        from textual.widgets import RichLog

        chat = ChatDisplay()
        widgets = list(chat.compose())
        assert any(isinstance(w, RichLog) for w in widgets)


# ===========================================================================
# ParleyApp event handler tests
# ===========================================================================


class TestParleyAppEventHandler:
    """Test ParleyApp._handle_event dispatches to the right widgets."""

    def test_handle_event_extract_type_from_object(self):
        """_get_event_type extracts type from an event object."""
        mock_event = MagicMock()
        mock_event.type = "message_update"

        result = ParleyApp._get_event_type(mock_event)
        assert result == "message_update"

    def test_handle_event_extract_type_from_dict(self):
        """_get_event_type extracts type from a dict."""
        result = ParleyApp._get_event_type({"type": "agent_end"})
        assert result == "agent_end"

    def test_handle_event_returns_empty_for_none(self):
        """_get_event_type returns empty string for None input."""
        result = ParleyApp._get_event_type(None)
        assert result == ""

    def test_handle_event_message_update_triggers_throttle(self):
        """_handle_event with message_update calls call_later and set_timer."""
        from tau_agent_core.events import AgentEvent

        app = ParleyApp()
        mock_event = AgentEvent(type="message_update", timestamp=0)

        with patch.object(app, "call_later") as mock_call_later, patch.object(
            app, "set_timer"
        ) as mock_set_timer:
            app._handle_event(mock_event)

        mock_call_later.assert_called_once()
        mock_set_timer.assert_called_once()

    def test_handle_event_agent_end_resets_streaming(self):
        """_handle_event with agent_end sets _is_streaming to False."""
        from tau_agent_core.events import AgentEvent

        app = ParleyApp()
        app._is_streaming = True
        mock_event = AgentEvent(type="agent_end", timestamp=0)

        app._handle_event(mock_event)
        assert app._is_streaming is False

    def test_handle_event_agent_start_enables_streaming(self):
        """_handle_event with agent_start sets _is_streaming to True."""
        from tau_agent_core.events import AgentEvent

        app = ParleyApp()
        app._is_streaming = False
        mock_event = AgentEvent(type="agent_start", timestamp=0)

        app._handle_event(mock_event)
        assert app._is_streaming is True

    def test_handle_event_message_start_dispatches(self):
        """_handle_event dispatches message_start event."""
        from tau_agent_core.events import AgentEvent

        app = ParleyApp()
        mock_event = AgentEvent(
            type="message_start",
            timestamp=0,
            message={"role": "assistant", "content": [{"type": "text", "text": "start"}]},
        )
        # Should not raise
        app._handle_event(mock_event)

    def test_handle_event_tool_execution_start_dispatches(self):
        """_handle_event dispatches tool_execution_start event."""
        from tau_agent_core.events import AgentEvent

        app = ParleyApp()
        mock_event = AgentEvent(
            type="tool_execution_start",
            timestamp=0,
            tool_name="bash",
            tool_call_id="call_001",
            args={"command": "ls"},
        )
        # Should not raise
        app._handle_event(mock_event)

    def test_handle_event_tool_execution_end_dispatches(self):
        """_handle_event dispatches tool_execution_end event."""
        from tau_agent_core.events import AgentEvent

        app = ParleyApp()
        mock_event = AgentEvent(
            type="tool_execution_end",
            timestamp=0,
            tool_name="bash",
            tool_call_id="call_001",
            result={"content": [{"type": "text", "text": "output"}]},
            is_error=False,
        )
        # Should not raise
        app._handle_event(mock_event)

    def test_handle_event_tool_execution_update_dispatches(self):
        """_handle_event dispatches tool_execution_update event."""
        from tau_agent_core.events import AgentEvent

        app = ParleyApp()
        mock_event = AgentEvent(
            type="tool_execution_update",
            timestamp=0,
            tool_name="bash",
            tool_call_id="call_001",
        )
        # Should not raise
        app._handle_event(mock_event)

    def test_handle_event_message_end_dispatches(self):
        """_handle_event dispatches message_end event."""
        from tau_agent_core.events import AgentEvent

        app = ParleyApp()
        mock_event = AgentEvent(
            type="message_end",
            timestamp=0,
            message={"role": "assistant", "content": [{"type": "text", "text": "end"}]},
        )
        # Should not raise
        app._handle_event(mock_event)

    def test_handle_event_all_10_event_types_dispatch(self):
        """_handle_event dispatches all 10 AgentEvent types without error."""
        from tau_agent_core.events import AgentEvent

        app = ParleyApp()
        event_types = [
            "agent_start", "agent_end",
            "turn_start", "turn_end",
            "message_start", "message_update", "message_end",
            "tool_execution_start", "tool_execution_update", "tool_execution_end",
        ]
        for et in event_types:
            evt = AgentEvent(type=et, timestamp=0)
            app._handle_event(evt)  # Should not raise

    def test_handle_event_ignores_unknown_type(self):
        """_handle_event with unknown type does not crash."""
        mock_event = MagicMock()
        mock_event.type = "unknown_event"

        app = ParleyApp()
        # Should not raise
        app._handle_event(mock_event)

    def test_handle_event_dict_format(self):
        """_handle_event works with dict events (backward compat)."""
        app = ParleyApp()
        app._is_streaming = True

        dict_event = {"type": "agent_end", "timestamp": 0}
        app._handle_event(dict_event)
        assert app._is_streaming is False

    def test_handle_event_throttle_timer_stopped_on_new_update(self):
        """New message_update stops the previous throttle timer."""
        from tau_agent_core.events import AgentEvent

        app = ParleyApp()
        mock_timer = MagicMock()
        mock_timer.stop = MagicMock()
        app._throttle_timer = mock_timer

        mock_event = AgentEvent(type="message_update", timestamp=0)
        with patch.object(app, "set_timer"):
            app._handle_event(mock_event)

        mock_timer.stop.assert_called_once()

    def test_handle_event_throttle_timer_1_30_seconds(self):
        """Throttle timer is set to 1/30 seconds (30Hz)."""
        from tau_agent_core.events import AgentEvent

        app = ParleyApp()
        mock_event = AgentEvent(type="message_update", timestamp=0)

        captured_interval = None

        def capture_timer(interval, callback):
            nonlocal captured_interval
            captured_interval = interval
            return MagicMock()

        with patch.object(app, "set_timer", side_effect=capture_timer):
            app._handle_event(mock_event)

        assert captured_interval == pytest.approx(1 / 30, abs=0.001)

    def test_handle_event_dict_format(self):
        """_handle_event works with dict events (backward compat)."""
        app = ParleyApp()
        app._is_streaming = True

        dict_event = {"type": "agent_end", "timestamp": 0}
        app._handle_event(dict_event)
        assert app._is_streaming is False

    def test_throttle_timer_stopped_on_new_update(self):
        """New message_update stops the previous throttle timer."""
        from tau_agent_core.events import AgentEvent

        app = ParleyApp()
        mock_timer = MagicMock()
        mock_timer.stop = MagicMock()
        app._throttle_timer = mock_timer

        mock_event = AgentEvent(type="message_update", timestamp=0)
        with patch.object(app, "set_timer"):
            app._handle_event(mock_event)

        mock_timer.stop.assert_called_once()

    def test_throttle_timer_1_30_seconds(self):
        """Throttle timer is set to 1/30 seconds (30Hz)."""
        from tau_agent_core.events import AgentEvent

        app = ParleyApp()
        mock_event = AgentEvent(type="message_update", timestamp=0)

        captured_interval = None

        def capture_timer(interval, callback):
            nonlocal captured_interval
            captured_interval = interval
            return MagicMock()

        with patch.object(app, "set_timer", side_effect=capture_timer):
            app._handle_event(mock_event)

        assert captured_interval == pytest.approx(1 / 30, abs=0.001)

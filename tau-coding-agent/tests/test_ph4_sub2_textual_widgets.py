"""Phase 4 Subphase 2 — Real Textual Widget Tests.

Tests actual Textual widget rendering using headless test driver.
Tests that the 6 widget classes render correctly and that ChatDisplay
manages them properly.

Reference: PHASE-4-SUBPHASE-2.md — Agent-Aware Widgets
Reference: docs/textual-headless-testing.md — Headless testing patterns
"""

from __future__ import annotations

import pytest
import textual.constants as _textual_constants

# Disable CSS loading to avoid CSS_PATH issues
_textual_constants.TEXTUAL_CSS = ""

from textual.app import App, ComposeResult
from textual.containers import Container
from textual.widgets import Label, Static, RichLog

# ---------------------------------------------------------------------------
# Widget imports (safe — no CSS_PATH issues)
# ---------------------------------------------------------------------------

from tau_coding_agent.widgets.chat_display import (
    ChatDisplay,
    UserMessageWidget,
    AssistantMessageWidget,
    ThinkingBlockWidget,
)
from tau_coding_agent.widgets.chat_display_data import ChatMessageData
from tau_coding_agent.widgets.tool_call_widget import ToolCallWidget, ToolCallData
from tau_coding_agent.widgets.tool_result_widget import ToolResultWidget, ToolResultData
from tau_coding_agent.widgets.footer import FooterWidget, FooterData

# ---------------------------------------------------------------------------
# Harness apps
# ---------------------------------------------------------------------------


class UserMessageWidgetHarness(App):
    """Harness for testing UserMessageWidget in isolation."""
    CSS = ""

    def compose(self) -> ComposeResult:
        data = ChatMessageData(
            role="user",
            content=[{"type": "text", "text": "Hello, agent!"}],
            timestamp=1700000000000,
        )
        yield UserMessageWidget(data)


class AssistantMessageWidgetHarness(App):
    """Harness for testing AssistantMessageWidget in isolation."""
    CSS = ""

    def compose(self) -> ComposeResult:
        data = ChatMessageData(
            role="assistant",
            content=[{"type": "text", "text": "I can help with that."}],
        )
        yield AssistantMessageWidget(data)


class ThinkingBlockWidgetHarness(App):
    """Harness for testing ThinkingBlockWidget in isolation."""
    CSS = ""

    def compose(self) -> ComposeResult:
        yield ThinkingBlockWidget("Let me reason through this...")


class ToolCallWidgetHarness(App):
    """Harness for testing ToolCallWidget in isolation."""
    CSS = ""

    def compose(self) -> ComposeResult:
        data = ToolCallData(
            tool_name="bash",
            tool_call_id="call_tc_001",
            arguments={"command": "ls -la /tmp"},
            status="running",
        )
        yield ToolCallWidget(data)


class ToolResultWidgetHarness(App):
    """Harness for testing ToolResultWidget in isolation."""
    CSS = ""

    def compose(self) -> ComposeResult:
        data = ChatMessageData(
            role="toolResult",
            content=[{"type": "text", "text": "total 4\ndrwxr-xr-x  2 user  64 Jan  1 12:00 .\ndrwxr-xr-x 10 user 640 Jan  1 11:00 .."}],
            tool_name="bash",
        )
        yield ToolResultWidget(data)


class FooterWidgetHarness(App):
    """Harness for testing FooterWidget in isolation."""
    CSS = ""

    def compose(self) -> ComposeResult:
        yield FooterWidget()

    def on_mount(self) -> None:
        self.query_one(FooterWidget).update(FooterData(
            model="gpt-4o",
            tokens=1500,
            context_percent=45,
            session_name="test-session",
        ))


class ChatDisplayHarness(App):
    """Harness for testing ChatDisplay with multiple widget types."""
    CSS = ""

    def compose(self) -> ComposeResult:
        yield ChatDisplay(id="chat-display")

    def on_mount(self) -> None:
        chat = self.query_one(ChatDisplay)

        # Append user message
        chat.append_message(ChatMessageData(
            role="user",
            content=[{"type": "text", "text": "Write a function."}],
            timestamp=1700000000000,
        ))

        # Append assistant message with text
        chat.append_message(ChatMessageData(
            role="assistant",
            content=[{"type": "text", "text": "Sure, let me write a function."}],
        ))

        # Append tool call
        chat.append_tool_call(ToolCallData(
            tool_name="bash",
            tool_call_id="call_001",
            arguments={"command": "cat file.txt"},
            status="running",
        ))

        # Append tool result
        chat.update_tool_result(ToolResultData(
            tool_name="bash",
            tool_call_id="call_001",
            result="hello world",
            is_error=False,
        ))

        # Append second assistant message
        chat.append_message(ChatMessageData(
            role="assistant",
            content=[{"type": "text", "text": "Done!"}],
        ))


class StreamingHarness(App):
    """Harness for streaming accumulation."""
    CSS = ""

    def compose(self) -> ComposeResult:
        yield ChatDisplay(id="chat-display")

    def on_mount(self) -> None:
        chat = self.query_one(ChatDisplay)
        chat.update_streaming_message(delta="H")
        chat.update_streaming_message(delta="i")
        chat.update_streaming_message(delta="!")


class FinalizeHarness(App):
    """Harness for testing finalize_streaming_message."""
    CSS = ""

    def compose(self) -> ComposeResult:
        yield ChatDisplay(id="chat-display")

    def on_mount(self) -> None:
        chat = self.query_one(ChatDisplay)
        chat.update_streaming_message(delta="partial")
        chat.finalize_streaming_message()


class TrackHarness(App):
    """Harness for testing tool call tracking."""
    CSS = ""

    def compose(self) -> ComposeResult:
        yield ChatDisplay(id="chat-display")

    def on_mount(self) -> None:
        chat = self.query_one(ChatDisplay)
        chat.append_tool_call(ToolCallData(
            tool_name="read",
            tool_call_id="read_001",
            arguments={"path": "/tmp/test.txt"},
            status="running",
        ))
        chat.append_tool_call(ToolCallData(
            tool_name="bash",
            tool_call_id="bash_001",
            arguments={"command": "ls"},
            status="pending",
        ))


class UpdateResultHarness(App):
    """Harness for testing update_tool_result."""
    CSS = ""

    def compose(self) -> ComposeResult:
        yield ChatDisplay(id="chat-display")

    def on_mount(self) -> None:
        chat = self.query_one(ChatDisplay)
        chat.update_tool_result(ToolResultData(
            tool_name="write",
            tool_call_id="write_001",
            result="Wrote 10 bytes to /tmp/test.txt",
            is_error=False,
        ))


class ErrorHarness(App):
    """Harness for testing error results."""
    CSS = ""

    def compose(self) -> ComposeResult:
        yield ToolResultWidget(ChatMessageData(
            role="toolResult",
            content=[{"type": "text", "text": "Error occurred"}],
            tool_name="bash",
            is_error=True,
        ))


class UpdateHarness(App):
    """Harness for testing footer update."""
    CSS = ""

    def compose(self) -> ComposeResult:
        yield FooterWidget(id="footer")

    def on_mount(self) -> None:
        self.query_one("#footer", FooterWidget).update(
            FooterData(model="claude-3.5-sonnet", tokens=5000)
        )


class StreamingEndHarness(App):
    """Harness for testing message_end finalizes streaming."""
    CSS = ""

    def compose(self) -> ComposeResult:
        yield ChatDisplay(id="chat-display")

    def on_mount(self) -> None:
        chat = self.query_one(ChatDisplay)
        chat.update_streaming_message(delta="partial")


class ToolExecHarness(App):
    """Harness for testing tool execution dispatch."""
    CSS = ""

    def compose(self) -> ComposeResult:
        yield ChatDisplay(id="chat-display")


class ToolExecEndHarness(App):
    """Harness for testing tool execution end dispatch."""
    CSS = ""

    def compose(self) -> ComposeResult:
        yield ChatDisplay(id="chat-display")


# ===========================================================================
# Test 1: UserMessageWidget renders correctly
# ===========================================================================


class TestUserMessageWidget:
    """Test UserMessageWidget renders user prompts."""

    async def test_widget_mounts_successfully(self):
        """UserMessageWidget mounts without error."""
        async with UserMessageWidgetHarness().run_test() as pilot:
            await pilot.pause()
            widgets = list(pilot.app.query(UserMessageWidget))
            assert len(widgets) == 1

    async def test_widget_has_correct_role_data(self):
        """UserMessageWidget stores the correct role data."""
        async with UserMessageWidgetHarness().run_test() as pilot:
            await pilot.pause()
            widget = pilot.app.query_one(UserMessageWidget)
            assert widget.data.role == "user"

    async def test_widget_has_text_content(self):
        """UserMessageWidget has a Markdown widget with text content."""
        from textual.widgets import Markdown

        async with UserMessageWidgetHarness().run_test() as pilot:
            await pilot.pause()
            widget = pilot.app.query_one(UserMessageWidget)
            markdown_widgets = list(widget.query(Markdown))
            assert len(markdown_widgets) >= 1

    async def test_widget_has_timestamp(self):
        """UserMessageWidget has a timestamp label."""
        async with UserMessageWidgetHarness().run_test() as pilot:
            await pilot.pause()
            widget = pilot.app.query_one(UserMessageWidget)
            timestamps = [l for l in widget.query(Label) if l.has_class("timestamp")]
            assert len(timestamps) == 1


# ===========================================================================
# Test 2: AssistantMessageWidget renders correctly
# ===========================================================================


class TestAssistantMessageWidget:
    """Test AssistantMessageWidget renders assistant responses."""

    async def test_widget_mounts_successfully(self):
        """AssistantMessageWidget mounts without error."""
        async with AssistantMessageWidgetHarness().run_test() as pilot:
            await pilot.pause()
            widget = pilot.app.query_one(AssistantMessageWidget)
            assert widget is not None

    async def test_widget_has_text_content(self):
        """AssistantMessageWidget has a Markdown widget with text."""
        from textual.widgets import Markdown

        async with AssistantMessageWidgetHarness().run_test() as pilot:
            await pilot.pause()
            widget = pilot.app.query_one(AssistantMessageWidget)
            markdown_widgets = list(widget.query(Markdown))
            assert len(markdown_widgets) >= 1

    async def test_widget_streaming_flag(self):
        """AssistantMessageWidget tracks streaming state."""
        async with AssistantMessageWidgetHarness().run_test() as pilot:
            await pilot.pause()
            widget = pilot.app.query_one(AssistantMessageWidget)
            assert widget._is_streaming is False

    async def test_streaming_widget_is_streaming(self):
        """AssistantMessageWidget created with streaming=True tracks it."""
        class StreamingTestHarness(App):
            CSS = ""
            def compose(self) -> ComposeResult:
                yield AssistantMessageWidget(ChatMessageData(
                    role="assistant",
                    content=[{"type": "text", "text": "streaming"}],
                    streaming=True,
                ))

        async with StreamingTestHarness().run_test() as pilot:
            await pilot.pause()
            widget = pilot.app.query_one(AssistantMessageWidget)
            assert widget._is_streaming is True

    async def test_append_text_updates_content(self):
        """AssistantMessageWidget.append_text accumulates text."""
        async with AssistantMessageWidgetHarness().run_test() as pilot:
            await pilot.pause()
            widget = pilot.app.query_one(AssistantMessageWidget)
            widget.append_text(" more text")
            assert " more text" in "".join(widget._text_parts)


# ===========================================================================
# Test 3: ThinkingBlockWidget is collapsible
# ===========================================================================


class TestThinkingBlockWidget:
    """Test ThinkingBlockWidget collapsibility."""

    async def test_widget_mounts_successfully(self):
        """ThinkingBlockWidget mounts without error."""
        async with ThinkingBlockWidgetHarness().run_test() as pilot:
            await pilot.pause()
            widget = pilot.app.query_one(ThinkingBlockWidget)
            assert widget is not None

    async def test_widget_starts_collapsed(self):
        """ThinkingBlockWidget starts in collapsed state."""
        async with ThinkingBlockWidgetHarness().run_test() as pilot:
            await pilot.pause()
            widget = pilot.app.query_one(ThinkingBlockWidget)
            assert widget._collapsed is True

    async def test_content_hidden_when_collapsed(self):
        """Content is hidden (display='none') when collapsed."""
        from textual.widgets import Markdown

        async with ThinkingBlockWidgetHarness().run_test() as pilot:
            await pilot.pause()
            widget = pilot.app.query_one(ThinkingBlockWidget)
            content = widget._content
            assert content.styles.display == "none"

    async def test_click_expands_widget(self):
        """Clicking ThinkingBlockWidget expands it."""
        async with ThinkingBlockWidgetHarness().run_test() as pilot:
            await pilot.pause()
            widget = pilot.app.query_one(ThinkingBlockWidget)
            widget.on_click()
            assert widget._collapsed is False
            assert widget._content.styles.display == "block"

    async def test_click_collapses_widget(self):
        """Clicking expanded ThinkingBlockWidget collapses it."""
        async with ThinkingBlockWidgetHarness().run_test() as pilot:
            await pilot.pause()
            widget = pilot.app.query_one(ThinkingBlockWidget)
            widget.on_click()  # expand
            widget.on_click()  # collapse
            assert widget._collapsed is True
            assert widget._content.styles.display == "none"

    async def test_header_label_toggles_text(self):
        """Clicking toggles the header text between expand/collapse."""
        async with ThinkingBlockWidgetHarness().run_test() as pilot:
            await pilot.pause()
            widget = pilot.app.query_one(ThinkingBlockWidget)
            header = widget._header
            assert header.content == "💭 [click to expand]"

            widget.on_click()
            assert header.content == "💭 [click to collapse]"

            widget.on_click()
            assert header.content == "💭 [click to expand]"


# ===========================================================================
# Test 4: ToolCallWidget shows status
# ===========================================================================


class TestToolCallWidget:
    """Test ToolCallWidget rendering and status updates."""

    async def test_widget_mounts_successfully(self):
        """ToolCallWidget mounts without error."""
        async with ToolCallWidgetHarness().run_test() as pilot:
            await pilot.pause()
            widget = pilot.app.query_one(ToolCallWidget)
            assert widget is not None

    async def test_widget_has_tool_name(self):
        """ToolCallWidget displays the tool name."""
        async with ToolCallWidgetHarness().run_test() as pilot:
            await pilot.pause()
            widget = pilot.app.query_one(ToolCallWidget)
            name_label = widget._tool_name_label
            assert name_label is not None
            assert "bash" in name_label.content

    async def test_widget_has_status_icon(self):
        """ToolCallWidget has a status icon label."""
        async with ToolCallWidgetHarness().run_test() as pilot:
            await pilot.pause()
            widget = pilot.app.query_one(ToolCallWidget)
            status_label = widget._status_label
            assert status_label is not None
            assert "🔄" in status_label.content

    async def test_running_status_shows_loader(self):
        """Running ToolCallWidget has a loader indicator."""
        from textual.widgets import Loader

        async with ToolCallWidgetHarness().run_test() as pilot:
            await pilot.pause()
            widget = pilot.app.query_one(ToolCallWidget)
            loader_widgets = list(widget.query(Loader))
            assert len(loader_widgets) == 1

    async def test_status_transition_pending(self):
        """ToolCallWidget status updates from running to pending."""
        async with ToolCallWidgetHarness().run_test() as pilot:
            await pilot.pause()
            widget = pilot.app.query_one(ToolCallWidget)
            widget.update_status("pending")
            assert widget._status == "pending"
            assert "⏳" in widget._status_label.content

    async def test_status_transition_done(self):
        """ToolCallWidget status updates to done."""
        async with ToolCallWidgetHarness().run_test() as pilot:
            await pilot.pause()
            widget = pilot.app.query_one(ToolCallWidget)
            widget.update_status("done")
            assert widget._status == "done"
            assert "✅" in widget._status_label.content

    async def test_status_transition_error(self):
        """ToolCallWidget status updates to error."""
        async with ToolCallWidgetHarness().run_test() as pilot:
            await pilot.pause()
            widget = pilot.app.query_one(ToolCallWidget)
            widget.update_status("error")
            assert widget._status == "error"
            assert "❌" in widget._status_label.content

    async def test_collapsible_arguments(self):
        """ToolCallWidget has a collapsible arguments section."""
        from textual.widgets import Collapsible

        async with ToolCallWidgetHarness().run_test() as pilot:
            await pilot.pause()
            widget = pilot.app.query_one(ToolCallWidget)
            collapsibles = list(widget.query(Collapsible))
            assert len(collapsibles) >= 1


# ===========================================================================
# Test 5: ToolResultWidget renders differently by tool type
# ===========================================================================


class TestToolResultWidget:
    """Test ToolResultWidget per-type rendering."""

    async def test_widget_mounts_successfully(self):
        """ToolResultWidget mounts without error."""
        async with ToolResultWidgetHarness().run_test() as pilot:
            await pilot.pause()
            widget = pilot.app.query_one(ToolResultWidget)
            assert widget is not None

    async def test_bash_result_has_markdown(self):
        """Bash result renders as Markdown."""
        from textual.widgets import Markdown

        async with ToolResultWidgetHarness().run_test() as pilot:
            await pilot.pause()
            widget = pilot.app.query_one(ToolResultWidget)
            markdown_widgets = list(widget.query(Markdown))
            assert len(markdown_widgets) >= 1

    async def test_tool_name_stored(self):
        """ToolResultWidget stores the tool name."""
        async with ToolResultWidgetHarness().run_test() as pilot:
            await pilot.pause()
            widget = pilot.app.query_one(ToolResultWidget)
            assert widget._tool_name == "bash"

    async def test_error_result_flag(self):
        """ToolResultWidget tracks error state."""
        async with ErrorHarness().run_test() as pilot:
            await pilot.pause()
            widget = pilot.app.query_one(ToolResultWidget)
            assert widget._is_error is True


# ===========================================================================
# Test 6: FooterWidget renders correctly
# ===========================================================================


class TestFooterWidget:
    """Test FooterWidget session info rendering."""

    async def test_widget_mounts_successfully(self):
        """FooterWidget mounts without error."""
        async with FooterWidgetHarness().run_test() as pilot:
            await pilot.pause()
            widget = pilot.app.query_one(FooterWidget)
            assert widget is not None

    async def test_widget_displays_model(self):
        """FooterWidget displays the model name."""
        async with FooterWidgetHarness().run_test() as pilot:
            await pilot.pause()
            widget = pilot.app.query_one(FooterWidget)
            assert "gpt-4o" in widget.content

    async def test_widget_displays_tokens(self):
        """FooterWidget displays token count."""
        async with FooterWidgetHarness().run_test() as pilot:
            await pilot.pause()
            widget = pilot.app.query_one(FooterWidget)
            assert "1500" in widget.content

    async def test_widget_displays_session_name(self):
        """FooterWidget displays session name."""
        async with FooterWidgetHarness().run_test() as pilot:
            await pilot.pause()
            widget = pilot.app.query_one(FooterWidget)
            assert "test-session" in widget.content

    async def test_widget_updates_on_data_change(self):
        """FooterWidget updates when new FooterData is set."""
        async with UpdateHarness().run_test() as pilot:
            await pilot.pause()
            widget = pilot.app.query_one(FooterWidget)
            assert "claude-3.5-sonnet" in widget.content


# ===========================================================================
# Test 7: ChatDisplay manages all widget types
# ===========================================================================


class TestChatDisplayManagement:
    """Test ChatDisplay manages all widget types correctly."""

    async def test_chat_display_mounts(self):
        """ChatDisplay with all widget types mounts without error."""
        async with ChatDisplayHarness().run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            chat = pilot.app.query_one(ChatDisplay)
            assert chat is not None

    async def test_chat_display_has_messages(self):
        """ChatDisplay tracks all appended messages."""
        async with ChatDisplayHarness().run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            chat = pilot.app.query_one(ChatDisplay)
            assert len(chat._messages) == 5  # user + assistant + tool_call + tool_result + assistant

    async def test_chat_display_streaming_accumulation(self):
        """ChatDisplay accumulates streaming text."""
        async with StreamingHarness().run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            chat = pilot.app.query_one(ChatDisplay)
            assert chat._streaming_message is not None
            full_text = "".join(chat._streaming_message._text_parts)
            assert full_text == "Hi!"

    async def test_chat_display_finalize_streaming(self):
        """ChatDisplay finalizes streaming message correctly."""
        async with FinalizeHarness().run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            chat = pilot.app.query_one(ChatDisplay)
            assert chat._streaming_message is None

    async def test_chat_display_tool_call_tracking(self):
        """ChatDisplay tracks tool call widgets by ID."""
        async with TrackHarness().run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            chat = pilot.app.query_one(ChatDisplay)
            assert len(chat._tool_call_widgets) == 2
            assert "read_001" in chat._tool_call_widgets
            assert "bash_001" in chat._tool_call_widgets

    async def test_chat_display_update_tool_result(self):
        """ChatDisplay creates ToolResultWidget on update."""
        async with UpdateResultHarness().run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            chat = pilot.app.query_one(ChatDisplay)
            tool_results = [w for w in chat._messages if isinstance(w, ToolResultWidget)]
            assert len(tool_results) == 1
            assert tool_results[0]._tool_name == "write"



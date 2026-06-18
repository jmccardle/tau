"""Phase 4 Subphase 2 — ParleyApp Event Dispatch Tests.

Tests ParleyApp._handle_event() dispatches events to correct widgets.
Imported from a separate module to avoid CSS_PATH inheritance issues.

Reference: PHASE-4-SUBPHASE-2.md — Event Handler Wiring
"""

from __future__ import annotations

import pytest
from textual.app import App, ComposeResult
from textual.containers import Container

from textual.widgets import RichLog


# ---------------------------------------------------------------------------
# Test harness apps
# ---------------------------------------------------------------------------


class TestApp(App):
    """Test app for dispatch tests."""
    CSS = ""

    def compose(self) -> ComposeResult:
        from tau_coding_agent.widgets.chat_display import ChatDisplay
        yield ChatDisplay(id="chat-display")


class StreamingEndApp(App):
    """Harness for testing message_end finalizes streaming."""
    CSS = ""

    def compose(self) -> ComposeResult:
        from tau_coding_agent.widgets.chat_display import ChatDisplay
        yield ChatDisplay(id="chat-display")

    def on_mount(self) -> None:
        from tau_coding_agent.widgets.chat_display import ChatDisplay
        chat = self.query_one(ChatDisplay)
        chat.update_streaming_message(delta="partial")


# ---------------------------------------------------------------------------
# Tests
# ===========================================================================


class TestParleyAppEventDispatch:
    """Test ParleyApp dispatches events to correct widgets."""

    async def test_handle_message_start_creates_widget(self):
        """message_start event creates a message widget."""
        from tau_agent_core.events import AgentEvent
        from tau_coding_agent.app import ParleyApp
        from tau_coding_agent.widgets.chat_display import ChatDisplay

        class DispatchApp(ParleyApp):
            CSS = ""

        app = DispatchApp()
        async with app.run_test() as pilot:
            await pilot.pause()
            event = AgentEvent(
                type="message_start",
                timestamp=0,
                message={"role": "user", "content": [{"type": "text", "text": "test"}]},
            )
            app._handle_event(event)
            await pilot.pause()
            chat = pilot.app.query_one(ChatDisplay)
            assert len(chat._messages) >= 1

    async def test_handle_message_end_finalizes_streaming(self):
        """message_end event finalizes the streaming message."""
        from tau_agent_core.events import AgentEvent
        from tau_coding_agent.app import ParleyApp
        from tau_coding_agent.widgets.chat_display import ChatDisplay

        class StreamingEndApp(ParleyApp):
            CSS = ""

            def compose(self) -> ComposeResult:
                from tau_coding_agent.widgets.chat_display import ChatDisplay
                yield ChatDisplay(id="chat-display")

            def on_mount(self) -> None:
                chat = self.query_one(ChatDisplay)
                chat.update_streaming_message(delta="partial")

        app = StreamingEndApp()
        async with app.run_test() as pilot:
            await pilot.pause()
            event = AgentEvent(type="message_end", timestamp=1)
            app._handle_event(event)
            await pilot.pause()
            chat = pilot.app.query_one(ChatDisplay)
            assert chat._streaming_message is None

    async def test_handle_tool_execution_creates_tool_call_widget(self):
        """tool_execution_start event creates a ToolCallWidget."""
        from tau_agent_core.events import AgentEvent
        from tau_coding_agent.app import ParleyApp
        from tau_coding_agent.widgets.chat_display import ChatDisplay
        from tau_coding_agent.widgets.tool_call_widget import ToolCallWidget

        class ToolExecApp(ParleyApp):
            CSS = ""

        app = ToolExecApp()
        async with app.run_test() as pilot:
            await pilot.pause()
            event = AgentEvent(
                type="tool_execution_start",
                timestamp=0,
                tool_name="bash",
                tool_call_id="call_test_01",
                args={"command": "echo hello"},
            )
            app._handle_event(event)
            await pilot.pause()
            chat = pilot.app.query_one(ChatDisplay)
            tool_calls = [w for w in chat._messages if isinstance(w, ToolCallWidget)]
            assert len(tool_calls) == 1
            assert tool_calls[0].data.tool_name == "bash"

    async def test_handle_tool_execution_end_creates_result(self):
        """tool_execution_end event creates a ToolResultWidget."""
        from tau_agent_core.events import AgentEvent
        from tau_coding_agent.app import ParleyApp
        from tau_coding_agent.widgets.chat_display import ChatDisplay
        from tau_coding_agent.widgets.tool_result_widget import ToolResultWidget

        class ToolExecEndApp(ParleyApp):
            CSS = ""

        app = ToolExecEndApp()
        async with app.run_test() as pilot:
            await pilot.pause()
            event = AgentEvent(
                type="tool_execution_end",
                timestamp=0,
                tool_name="write",
                tool_call_id="write_test_01",
                result={"content": "Wrote file"},
                is_error=False,
            )
            app._handle_event(event)
            await pilot.pause()
            chat = pilot.app.query_one(ChatDisplay)
            results = [w for w in chat._messages if isinstance(w, ToolResultWidget)]
            assert len(results) == 1
            assert results[0]._tool_name == "write"

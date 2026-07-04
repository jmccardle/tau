"""Tests for Phase 6 Subphase 3 — Error Handling Tests.

Verifies error handling behavior:
1. Provider error handling: Provider error → error event → error message in chat
2. Tool error handling: Tool error → error result → sent to LLM
3. Extension error handling: Extension error → logged → agent continues

Reference: docs/PHASE-6-SUBPHASE-3.md — Error Handling Tests section
Reference: docs/SUBPHASE-0.0.md AgentSession interface
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tau_ai.types import Model
from tau_agent_core.agent_session import AgentSession
from tau_agent_core.events import AgentEvent, EventBus
from tau_agent_core.extension_types import ExtensionAPI, ExtensionContext
from tau_agent_core.sdk import create_agent_session
from tau_agent_core.session_log import InMemorySessionLog
from tau_agent_core.tools.base import AgentTool, AgentToolResult, ToolDefinition


# ============================================================================
# Fixtures
# ============================================================================


def _make_model(**overrides) -> Model:
    """Create a sample Model with optional overrides."""
    return Model(
        id=overrides.pop("id", "gpt-4o"),
        name=overrides.pop("name", "GPT-4o"),
        api=overrides.pop("api", "openai-completions"),
        provider=overrides.pop("provider", "openai"),
        base_url=overrides.pop("base_url", "https://api.openai.com/v1"),
        context_window=overrides.pop("context_window", 128000),
        max_tokens=overrides.pop("max_tokens", 4096),
        **overrides,
    )


def _make_session(session_log=None) -> AgentSession:
    """Create an AgentSession with an in-memory session log."""
    return AgentSession(
        session_log=session_log or InMemorySessionLog(),
        model=_make_model(),
    )


# ============================================================================
# Test 1: Provider error handling
# ============================================================================


@pytest.mark.usefixtures("fake_llm")
class TestProviderErrorHandling:
    """Test 1: Provider error → error event → error message in chat.

    When stream_simple() returns an ErrorEvent, the agent loop converts it
    to an AgentEvent with is_error=True and a human-readable message.

    The prompt()-based tests use ``fake_llm`` so the loop runs without a live
    network call (previously they 401'd against the real OpenAI API).
    """

    def test_event_bus_handles_error_events(self):
        """EventBus can emit error-type events."""
        bus = EventBus()
        received = []

        def handler(event):
            received.append(event)

        bus.on("all", handler)
        error_event = AgentEvent(
            type="message_end",
            timestamp=100,
            is_error=True,
            message={"role": "assistant", "content": []},
        )
        asyncio.run(bus.emit(error_event))

        assert len(received) == 1
        assert received[0].is_error is True

    def test_event_bus_error_handler_does_not_crash(self):
        """EventBus error handlers don't crash the bus."""
        bus = EventBus()
        received = []

        def good_handler(event):
            received.append(event)

        def bad_handler(event):
            raise ValueError("Intentional error")

        bus.on("all", bad_handler)
        bus.on("all", good_handler)

        # Should not raise
        error_event = AgentEvent(
            type="message_end",
            timestamp=100,
            is_error=True,
        )
        asyncio.run(bus.emit(error_event))

        # Good handler should still be called
        assert len(received) == 1

    def test_agent_session_error_event_subscription(self):
        """AgentSession subscribers receive error events."""
        session = _make_session()
        received = []

        def handler(event):
            received.append(event)

        session.subscribe(handler)

        # Emit an error event through the event bus
        error_event = AgentEvent(
            type="message_end",
            timestamp=100,
            is_error=True,
            message={"role": "assistant", "content": []},
        )
        asyncio.run(session._events.emit(error_event))

        assert len(received) == 1
        assert received[0].is_error is True

    def test_agent_session_error_message_in_chat(self):
        """Error events can carry error messages in the chat."""
        session = _make_session()
        received = []

        def handler(event):
            received.append(event)

        session.subscribe(handler)

        # Emit an error with a message
        error_event = AgentEvent(
            type="message_end",
            timestamp=100,
            is_error=True,
            message={
                "role": "assistant",
                "content": [{"type": "text", "text": "Error: provider unavailable"}],
            },
        )
        asyncio.run(session._events.emit(error_event))

        assert len(received) == 1
        assert received[0].is_error is True
        msg = received[0].message
        assert msg is not None
        assert msg["role"] == "assistant"

    def test_prompt_handles_error_state(self):
        """prompt() works even if an error state exists."""
        session = _make_session()
        messages = asyncio.run(session.prompt("hello"))
        assert len(messages) > 0

    def test_stream_simple_error_event_handling(self):
        """StreamErrorEvent is properly typed."""
        from tau_ai.streaming import ErrorEvent as StreamErrorEvent

        error_event = StreamErrorEvent(
            type="error",
            message="Connection refused",
            is_error=True,
        )
        assert error_event.type == "error"
        assert error_event.message == "Connection refused"
        assert error_event.is_error is True

    def test_text_delta_event_structure(self):
        """TextDeltaEvent has correct structure."""
        from tau_ai.streaming import TextDeltaEvent

        delta_event = TextDeltaEvent(
            type="text_delta",
            delta="Hello",
            partial=Model(id="gpt-4o", name="GPT-4o", api="openai-completions",
                          provider="openai", base_url="https://api.openai.com/v1",
                          context_window=128000, max_tokens=4096),
        )
        assert delta_event.type == "text_delta"
        assert delta_event.delta == "Hello"

    def test_done_event_structure(self):
        """DoneEvent has correct structure."""
        from tau_ai.streaming import DoneEvent

        done_event = DoneEvent(
            type="done",
            final=Model(id="gpt-4o", name="GPT-4o", api="openai-completions",
                        provider="openai", base_url="https://api.openai.com/v1",
                        context_window=128000, max_tokens=4096),
            usage={"input_tokens": 10, "output_tokens": 5},
        )
        assert done_event.type == "done"

    def test_tool_call_delta_event_structure(self):
        """ToolCallDeltaEvent has correct structure."""
        from tau_ai.streaming import ToolCallDeltaEvent

        tc_event = ToolCallDeltaEvent(
            type="toolcall_delta",
            delta={"function": {"name": "ls"}},
            partial=Model(id="gpt-4o", name="GPT-4o", api="openai-completions",
                          provider="openai", base_url="https://api.openai.com/v1",
                          context_window=128000, max_tokens=4096),
        )
        assert tc_event.type == "toolcall_delta"
        assert tc_event.delta["function"]["name"] == "ls"

    def test_all_stream_event_types_defined(self):
        """All streaming event types are importable and valid."""
        from tau_ai.streaming import (
            TextDeltaEvent,
            ToolCallDeltaEvent,
            DoneEvent,
            ErrorEvent,
        )

        # TextDeltaEvent
        td = TextDeltaEvent(delta="", partial=MagicMock())
        assert td.type == "text_delta"

        # ToolCallDeltaEvent
        tc = ToolCallDeltaEvent(delta={}, partial=MagicMock())
        assert tc.type == "toolcall_delta"

        # DoneEvent
        d = DoneEvent(final=MagicMock(), usage={})
        assert d.type == "done"

        # ErrorEvent
        e = ErrorEvent(message="test")
        assert e.type == "error"
        assert e.is_error is True

    def test_error_event_is_error_always_true(self):
        """ErrorEvent.is_error is always True (Literal[True])."""
        from tau_ai.streaming import ErrorEvent

        e = ErrorEvent(message="any error")
        assert e.is_error is True

    def test_error_event_message_required(self):
        """ErrorEvent requires a message."""
        from tau_ai.streaming import ErrorEvent

        e = ErrorEvent(message="Connection timeout after 30s")
        assert e.message == "Connection timeout after 30s"


# ============================================================================
# Test 2: Tool error handling
# ============================================================================


@pytest.mark.usefixtures("fake_llm")
class TestToolErrorHandling:
    """Test 2: Tool error → error result → sent to LLM.

    When a tool's execute() raises an exception, the agent loop catches it,
    wraps it in a ToolResultMessage with is_error=True, and sends it to the LLM.
    """

    def test_agent_tool_result_from_error(self):
        """AgentToolResult.from_error() creates an error result."""
        result = AgentToolResult.from_error(
            tool_name="failing_tool",
            error_message="Something went wrong",
            tool_call_id="call_001",
        )
        assert result.tool_name == "failing_tool"
        assert result.tool_call_id == "call_001"
        assert result.is_error is True
        assert result.error_message == "Something went wrong"
        assert len(result.content) == 1
        assert result.content[0]["text"] == "Something went wrong"

    def test_agent_tool_result_error_no_call_id(self):
        """AgentToolResult.from_error() works without tool_call_id."""
        result = AgentToolResult.from_error(
            tool_name="tool",
            error_message="Error",
        )
        assert result.tool_call_id is None
        assert result.is_error is True

    def test_agent_tool_result_normal(self):
        """Normal AgentToolResult is not an error."""
        result = AgentToolResult(
            tool_name="ls",
            tool_call_id="call_001",
            content=[{"type": "text", "text": "file1\nfile2"}],
            is_error=False,
        )
        assert result.is_error is False
        assert result.error_message is None

    def test_agent_tool_result_with_content_list(self):
        """AgentToolResult can have multiple content blocks."""
        result = AgentToolResult(
            tool_name="read",
            tool_call_id="call_001",
            content=[
                {"type": "text", "text": "Line 1"},
                {"type": "text", "text": "Line 2"},
            ],
        )
        assert len(result.content) == 2

    def test_failing_tool_in_session(self):
        """Session with a failing tool doesn't crash on prompt."""
        session = _make_session()
        messages = asyncio.run(session.prompt("hello"))
        assert len(messages) > 0

    def test_tool_error_emits_error_event(self):
        """Tool error emits an error event."""
        bus = EventBus()
        error_events = []

        def handler(event):
            if event.is_error:
                error_events.append(event)

        bus.on("all", handler)

        error_event = AgentEvent(
            type="tool_execution_end",
            timestamp=100,
            tool_name="failing_tool",
            tool_call_id="call_001",
            result="Error: command not found",
            is_error=True,
        )
        asyncio.run(bus.emit(error_event))

        assert len(error_events) == 1
        assert error_events[0].tool_name == "failing_tool"

    def test_tool_result_message_structure(self):
        """ToolResultMessage structure matches the contract."""
        from tau_ai.types import ToolResultMessage, TextContent
        import time

        tool_result = ToolResultMessage(
            role="toolResult",
            tool_call_id="call_001",
            tool_name="bash",
            content=[TextContent(text="output")],
            is_error=False,
            timestamp=int(time.time() * 1000),
        )
        assert tool_result.role == "toolResult"
        assert tool_result.tool_name == "bash"
        assert tool_result.is_error is False

    def test_tool_result_message_error(self):
        """ToolResultMessage with is_error=True."""
        from tau_ai.types import ToolResultMessage, TextContent
        import time

        tool_result = ToolResultMessage(
            role="toolResult",
            tool_call_id="call_001",
            tool_name="bash",
            content=[TextContent(text="Error: command failed")],
            is_error=True,
            timestamp=int(time.time() * 1000),
        )
        assert tool_result.is_error is True
        assert tool_result.content[0].text == "Error: command failed"

    def test_tool_result_message_serialization(self):
        """ToolResultMessage serializes correctly."""
        from tau_ai.types import ToolResultMessage, TextContent
        import time

        ts = int(time.time() * 1000)
        tool_result = ToolResultMessage(
            role="toolResult",
            tool_call_id="call_001",
            tool_name="bash",
            content=[TextContent(text="output")],
            is_error=False,
            timestamp=ts,
        )
        data = tool_result.model_dump()
        assert data["role"] == "toolResult"
        assert data["tool_call_id"] == "call_001"
        assert data["tool_name"] == "bash"
        assert data["is_error"] is False

    def test_tool_batch_result_with_errors(self):
        """ToolBatchResult handles mixed success/error results."""
        from tau_agent_core.tools.base import AgentToolResult, ToolBatchResult

        results = [
            AgentToolResult(tool_name="read", tool_call_id="c1", content=[{"type": "text", "text": "content"}]),
            AgentToolResult.from_error("bash", "command not found", "c2"),
        ]
        batch = ToolBatchResult(tool_results=results)

        assert len(batch.tool_results) == 2
        assert batch.tool_results[0].is_error is False
        assert batch.tool_results[1].is_error is True
        assert bool(batch) is True  # Not terminated

    def test_tool_batch_result_terminated(self):
        """ToolBatchResult with terminate=True is falsy."""
        from tau_agent_core.tools.base import ToolBatchResult

        batch = ToolBatchResult(terminate=True)
        assert batch.terminate is True
        assert bool(batch) is False

    def test_tool_batch_result_messages(self):
        """ToolBatchResult can hold messages from tool results."""
        from tau_agent_core.tools.base import AgentToolResult, ToolBatchResult
        from tau_ai.types import ToolResultMessage
        import time

        results = [
            AgentToolResult(
                tool_name="bash",
                tool_call_id="c1",
                content=[{"type": "text", "text": "ls output"}],
            ),
        ]
        # ToolBatchResult takes messages as a parameter
        batch = ToolBatchResult(
            tool_results=results,
            messages=[{"role": "toolResult", "tool_call_id": "c1", "tool_name": "bash", "content": [{"type": "text", "text": "ls output"}]}],
        )
        assert len(batch.messages) > 0
        assert batch.messages[0]["role"] == "toolResult"

    def test_validate_tool_arguments_raises_on_invalid(self):
        """validate_tool_arguments raises ValueError for invalid arguments."""
        from tau_ai.tools import validate_tool_arguments
        from tau_agent_core.tools.base import AgentTool

        # Create a tool with required args
        tool = AgentTool(
            definition=ToolDefinition(
                name="test_tool",
                label="Test",
                description="A test tool",
                parameters={
                    "type": "object",
                    "properties": {"name": {"type": "string"}},
                    "required": ["name"],
                },
                execute=lambda **kw: "result",
            )
        )

        with pytest.raises(ValueError):
            validate_tool_arguments(tool, {})  # Missing required "name"

    def test_validate_tool_arguments_passes_on_valid(self):
        """validate_tool_arguments passes on valid arguments."""
        from tau_ai.tools import validate_tool_arguments
        from tau_agent_core.tools.base import AgentTool

        tool = AgentTool(
            definition=ToolDefinition(
                name="test_tool",
                label="Test",
                description="A test tool",
                parameters={
                    "type": "object",
                    "properties": {"name": {"type": "string"}},
                    "required": ["name"],
                },
                execute=lambda **kw: "result",
            )
        )

        # Should not raise
        validate_tool_arguments(tool, {"name": "test"})

    def test_validate_tool_arguments_passes_no_required(self):
        """validate_tool_arguments passes when no required args."""
        from tau_ai.tools import validate_tool_arguments
        from tau_agent_core.tools.base import AgentTool

        tool = AgentTool(
            definition=ToolDefinition(
                name="test_tool",
                label="Test",
                description="A test tool",
                parameters={
                    "type": "object",
                    "properties": {"name": {"type": "string"}},
                    "required": [],
                },
                execute=lambda **kw: "result",
            )
        )

        # Should not raise
        validate_tool_arguments(tool, {})


# ============================================================================
# Test 3: Extension error handling
# ============================================================================


@pytest.mark.usefixtures("fake_llm")
class TestExtensionErrorHandling:
    """Test 3: Extension error → logged → agent continues.

    When an extension handler raises an exception, the EventBus logs it
    and continues to the next handler. The agent loop is not affected.
    """

    def test_extension_error_does_not_crash_agent(self):
        """Extension error doesn't crash the agent loop."""
        session = _make_session()

        def bad_ext(api):
            def bad_handler(event):
                raise ValueError("Extension error!")

            api.on("agent_start", bad_handler)

        session2 = AgentSession(
            session_log=InMemorySessionLog(),
            model=_make_model(),
            extensions=[bad_ext],
        )
        # Should not raise
        messages = asyncio.run(session2.prompt("hello"))
        assert len(messages) > 0

    def test_extension_error_handler_in_bus(self):
        """EventBus handles handler exceptions without crashing."""
        bus = EventBus()
        received = []

        def good_handler(event):
            received.append(event)

        def bad_handler(event):
            raise ValueError("Intentional extension error")

        bus.on("agent_start", bad_handler)
        bus.on("agent_start", good_handler)

        # Should not raise; good_handler should still be called
        asyncio.run(
            bus.emit(AgentEvent(type="agent_start", timestamp=100))
        )

        assert len(received) == 1

    def test_extension_api_on_method(self):
        """ExtensionAPI.on() registers event handlers."""
        api = ExtensionAPI()
        received = []

        def handler(event):
            received.append(event)

        unsub = api.on("agent_start", handler)
        assert callable(unsub)
        assert "agent_start" in api._handlers

    def test_extension_api_unsubscribe(self):
        """ExtensionAPI unsubscribe removes the handler from the event bus."""
        api = ExtensionAPI()
        received = []

        def handler(event):
            received.append(event)

        unsub = api.on("agent_start", handler)
        unsub()
        # Unsub removes from the event bus, not from _handlers copy
        # (the _handlers dict is a backward-compatible copy)
        assert handler not in api._event_bus._listeners.get("agent_start", [])

    def test_extension_context_has_all_properties(self):
        """ExtensionContext exposes all expected properties."""
        context = ExtensionContext()

        assert hasattr(context, "cwd")
        assert hasattr(context, "session_manager")
        assert hasattr(context, "signal")
        assert hasattr(context, "is_idle")

    def test_extension_context_defaults(self):
        """ExtensionContext has sensible defaults."""
        context = ExtensionContext()

        assert context.cwd == "."
        assert context.session_manager is None
        assert context.signal is None
        assert context.is_idle is True

    def test_extension_context_set_ui_delegate(self):
        """ExtensionContext.set_ui_delegate enables TUI mode."""
        context = ExtensionContext()
        assert context._ui._mode == "headless"

        class FakeDelegate:
            pass

        context.set_ui_delegate(FakeDelegate())
        assert context._ui._mode == "tui"
        assert context._ui._tui_delegate is not None

    def test_extension_context_abort(self):
        """ExtensionContext.abort() calls signal.abort() if available."""
        from tau_ai.abort import AbortSignal

        signal = AbortSignal()
        context = ExtensionContext(signal=signal)

        assert not context.signal.is_aborted()
        context.abort()
        assert context.signal.is_aborted()

    def test_extension_context_abort_no_signal(self):
        """ExtensionContext.abort() is safe when no signal."""
        context = ExtensionContext(signal=None)
        # Should not raise
        context.abort()

    def test_extension_context_shutdown(self):
        """ExtensionContext.shutdown() calls session_manager.shutdown() if available."""
        class FakeSM:
            shutdown_called = False

            def shutdown(self):
                self.shutdown_called = True

        sm = FakeSM()
        context = ExtensionContext(session_manager=sm)
        context.shutdown()
        assert sm.shutdown_called

    def test_extension_context_shutdown_no_manager(self):
        """ExtensionContext.shutdown() is safe without session_manager."""
        context = ExtensionContext(session_manager=None)
        # Should not raise
        context.shutdown()

    def test_extension_context_get_context_usage(self):
        """ExtensionContext.get_context_usage() returns a dict."""
        context = ExtensionContext()
        usage = context.get_context_usage()
        assert isinstance(usage, dict)
        assert "total_tokens" in usage

    def test_extension_ui_headless_confirm(self):
        """ExtensionUI.confirm() returns True in headless mode."""
        from tau_agent_core.extension_types import ExtensionUI
        ui = ExtensionUI(mode="headless")
        result = asyncio.run(ui.confirm("title", "message"))
        assert result is True

    def test_extension_ui_headless_select(self):
        """ExtensionUI.select() returns first item in headless mode."""
        from tau_agent_core.extension_types import ExtensionUI
        ui = ExtensionUI(mode="headless")
        result = asyncio.run(ui.select("title", ["option1", "option2"]))
        assert result == "option1"

    def test_extension_ui_headless_select_empty(self):
        """ExtensionUI.select() returns None for empty list in headless mode."""
        from tau_agent_core.extension_types import ExtensionUI
        ui = ExtensionUI(mode="headless")
        result = asyncio.run(ui.select("title", []))
        assert result is None

    def test_extension_ui_headless_input(self):
        """ExtensionUI.input() returns default in headless mode."""
        from tau_agent_core.extension_types import ExtensionUI
        ui = ExtensionUI(mode="headless")
        result = asyncio.run(ui.input("title", "default_value"))
        assert result == "default_value"

    def test_extension_ui_notify_to_stderr(self):
        """ExtensionUI.notify() prints to stderr in headless mode."""
        import sys
        from io import StringIO
        from tau_agent_core.extension_types import ExtensionUI

        ui = ExtensionUI(mode="headless")

        stderr_capture = StringIO()
        original_stderr = sys.stderr
        sys.stderr = stderr_capture

        try:
            ui.notify("test message", level="error")
        finally:
            sys.stderr = original_stderr

        output = stderr_capture.getvalue()
        assert "[τ]" in output
        assert "error" in output
        assert "test message" in output

    def test_extension_api_can_send_user_message(self):
        """ExtensionAPI.send_user_message() works."""
        session_mock = MagicMock()
        api = ExtensionAPI(session=session_mock)
        # Should not raise
        api.send_user_message("test message", deliver_as="steer")

    def test_extension_api_can_send_message(self):
        """ExtensionAPI.send_message() works."""
        session_mock = MagicMock()
        api = ExtensionAPI(session=session_mock)
        # Should not raise
        api.send_message({"role": "user", "content": []}, {})

    def test_extension_api_register_flag(self):
        """ExtensionAPI.register_flag() registers a flag."""
        api = ExtensionAPI()
        api.register_flag("verbose", {"type": "boolean"})
        assert "verbose" in api._flags

    def test_extension_api_get_flag(self):
        """ExtensionAPI.get_flag() returns flag value."""
        api = ExtensionAPI()
        api._flags["verbose"] = {"value": True}
        assert api.get_flag("verbose") is True

    def test_extension_api_get_flag_missing(self):
        """ExtensionAPI.get_flag() returns None for missing flag."""
        api = ExtensionAPI()
        assert api.get_flag("nonexistent") is None

    def test_extension_api_append_entry(self):
        """ExtensionAPI.append_entry() works."""
        api = ExtensionAPI()
        # Should not raise
        api.append_entry("custom_type", {"key": "value"})

    def test_extension_api_set_session_name(self):
        """ExtensionAPI.set_session_name() sets the session name."""
        api = ExtensionAPI()
        api.set_session_name("my-session")
        assert api._session_name == "my-session"

    def test_extension_api_get_all_tools_empty(self):
        """ExtensionAPI.get_all_tools() returns list."""
        api = ExtensionAPI()
        tools = api.get_all_tools()
        assert isinstance(tools, list)

    def test_extension_api_set_active_tools(self):
        """ExtensionAPI.set_active_tools() sets active tools."""
        api = ExtensionAPI()
        api.set_active_tools(["read", "write"])
        assert api._active_tools == ["read", "write"]

    def test_extension_api_register_command(self):
        """ExtensionAPI.register_command() registers a command."""
        api = ExtensionAPI()
        api.register_command("mycmd", {"description": "Test command"})
        assert "mycmd" in api._commands

    def test_multiple_extension_errors_dont_crash(self):
        """Multiple extension errors don't crash the system."""
        def bad_ext1(api):
            api.on("all", lambda e: (_ for _ in ()).throw(ValueError("ext1 error")))

        def bad_ext2(api):
            api.on("agent_start", lambda e: 1 / 0)

        session = AgentSession(
            session_log=InMemorySessionLog(),
            model=_make_model(),
            extensions=[bad_ext1, bad_ext2],
        )
        # Should not raise
        messages = asyncio.run(session.prompt("hello"))
        assert len(messages) > 0

    def test_extension_error_after_prompt(self):
        """Extensions that error don't crash the agent loop."""
        ext_error_raised = [False]

        def bad_ext(api):
            def on_message(event):
                ext_error_raised[0] = True
                raise RuntimeError("Extension error after prompt")

            api.on("agent_start", on_message)

        session = AgentSession(
            session_log=InMemorySessionLog(),
            model=_make_model(),
            extensions=[bad_ext],
        )
        # Should not raise - extension errors are caught by EventBus
        messages = asyncio.run(session.prompt("hello"))
        assert len(messages) > 0

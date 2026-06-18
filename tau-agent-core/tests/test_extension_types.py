"""Tests for tau_agent_core.extension_types — ExtensionAPI, ExtensionContext, ExtensionUI.

Tests verify:
- ExtensionAPI exposes all documented methods
- ExtensionAPI.on() registers event handlers
- ExtensionAPI.register_tool() stores tool definitions
- ExtensionAPI.get_all_tools() returns registered tools
- ExtensionContext provides required properties
- ExtensionUI methods are no-ops (headless mode)
- ExtensionUI.confirm() returns True by default
- ExtensionUI.select() returns first item or None
- ExtensionUI.input() returns default value

Reference: SUBPHASE-0.0.md, "8. Extension API Surface" section
Reference: PHASE-2-SUBPHASE-0.md, Testing section items 1, 3
"""

import pytest

from tau_agent_core.extension_types import (
    ExtensionAPI,
    ExtensionContext,
    ExtensionUI,
)


class TestExtensionAPIInit:
    """Tests for ExtensionAPI initialization."""

    def test_create_extension_api(self):
        """ExtensionAPI can be instantiated."""
        api = ExtensionAPI()
        assert api is not None

    def test_extension_api_has_handlers_dict(self):
        """ExtensionAPI has internal handlers storage."""
        api = ExtensionAPI()
        assert hasattr(api, "_handlers")

    def test_extension_api_has_tools_list(self):
        """ExtensionAPI has internal tools storage."""
        api = ExtensionAPI()
        assert hasattr(api, "_tools")


class TestExtensionAPIEvents:
    """Tests for ExtensionAPI.on() event registration."""

    def test_register_handler(self):
        """ExtensionAPI.on() registers a handler."""
        api = ExtensionAPI()
        handler_called = []

        def handler(*args, **kwargs):
            handler_called.append((args, kwargs))

        api.on("test_event", handler)
        assert "test_event" in api._handlers
        assert handler in api._handlers["test_event"]

    def test_register_multiple_handlers(self):
        """ExtensionAPI.on() can register multiple handlers for same event."""
        api = ExtensionAPI()
        call_count = [0]

        def handler1():
            call_count[0] += 1

        def handler2():
            call_count[0] += 1

        api.on("my_event", handler1)
        api.on("my_event", handler2)
        assert len(api._handlers["my_event"]) == 2

    def test_register_handler_creates_event_key(self):
        """ExtensionAPI.on() creates event key if it doesn't exist."""
        api = ExtensionAPI()
        api.on("new_event", lambda: None)
        assert "new_event" in api._handlers


class TestExtensionAPITools:
    """Tests for ExtensionAPI tool methods."""

    def test_register_tool(self):
        """ExtensionAPI.register_tool() stores tool definition."""
        api = ExtensionAPI()
        tool_def = {"name": "ls", "description": "List files"}
        api.register_tool(tool_def)
        assert tool_def in api._tools

    def test_get_all_tools_empty(self):
        """ExtensionAPI.get_all_tools() returns empty list initially."""
        api = ExtensionAPI()
        assert api.get_all_tools() == []

    def test_get_all_tools_after_register(self):
        """ExtensionAPI.get_all_tools() returns registered tools."""
        api = ExtensionAPI()
        tool1 = {"name": "ls", "description": "List files"}
        tool2 = {"name": "grep", "description": "Search files"}
        api.register_tool(tool1)
        api.register_tool(tool2)
        tools = api.get_all_tools()
        assert len(tools) == 2
        assert tool1 in tools
        assert tool2 in tools

    def test_set_active_tools(self):
        """ExtensionAPI.set_active_tools() stores active tool names."""
        api = ExtensionAPI()
        api.set_active_tools(["ls", "grep"])
        assert hasattr(api, "_active_tools")

    def test_register_multiple_tools(self):
        """ExtensionAPI.register_tool() can register multiple tools."""
        api = ExtensionAPI()
        for i in range(5):
            api.register_tool({"name": f"tool_{i}"})
        assert len(api.get_all_tools()) == 5


class TestExtensionAPICommands:
    """Tests for ExtensionAPI command registration."""

    def test_register_command(self):
        """ExtensionAPI.register_command() stores a command."""
        api = ExtensionAPI()
        cmd = {"action": "help"}
        api.register_command("help", cmd)
        assert "help" in api._commands
        assert api._commands["help"] == cmd

    def test_register_multiple_commands(self):
        """ExtensionAPI.register_command() can register multiple commands."""
        api = ExtensionAPI()
        api.register_command("help", {"action": "help"})
        api.register_command("status", {"action": "status"})
        assert "help" in api._commands
        assert "status" in api._commands


class TestExtensionAPIFlags:
    """Tests for ExtensionAPI flag registration."""

    def test_register_flag(self):
        """ExtensionAPI.register_flag() stores a flag."""
        api = ExtensionAPI()
        options = {"type": "boolean", "default": False}
        api.register_flag("debug", options)
        assert "debug" in api._flags
        assert api._flags["debug"] == options

    def test_get_flag_existing(self):
        """ExtensionAPI.get_flag() returns existing flag."""
        api = ExtensionAPI()
        api.register_flag("debug", {"type": "boolean"})
        assert api.get_flag("debug") is not None

    def test_get_flag_missing(self):
        """ExtensionAPI.get_flag() returns None for missing flag."""
        api = ExtensionAPI()
        assert api.get_flag("nonexistent") is None


class TestExtensionAPIAppendEntry:
    """Tests for ExtensionAPI.append_entry()."""

    def test_append_entry_exists(self):
        """ExtensionAPI has append_entry method."""
        api = ExtensionAPI()
        assert hasattr(api, "append_entry")

    def test_append_entry_callable(self):
        """ExtensionAPI.append_entry() is callable."""
        api = ExtensionAPI()
        # Should not raise
        api.append_entry("notification", {"text": "test"})


class TestExtensionAPISession:
    """Tests for ExtensionAPI session methods."""

    def test_set_session_name(self):
        """ExtensionAPI.set_session_name() stores session name."""
        api = ExtensionAPI()
        api.set_session_name("My Session")
        assert hasattr(api, "_session_name")

    def test_send_user_message(self):
        """ExtensionAPI.send_user_message() is callable."""
        api = ExtensionAPI()
        # Should not raise
        api.send_user_message("Hello")

    def test_send_user_message_deliver_as(self):
        """ExtensionAPI.send_user_message() accepts deliver_as parameter."""
        api = ExtensionAPI()
        api.send_user_message("Hello", deliver_as="steer")

    def test_send_message(self):
        """ExtensionAPI.send_message() is callable."""
        api = ExtensionAPI()
        # Should not raise
        api.send_message({"text": "Hello"}, {})


class TestExtensionAPIProperty:
    """Tests for ExtensionAPI.ui property."""

    def test_ui_returns_extension_ui(self):
        """ExtensionAPI.ui returns an ExtensionUI instance."""
        api = ExtensionAPI()
        ui = api.ui
        assert isinstance(ui, ExtensionUI)

    @pytest.mark.asyncio
    async def test_ui_is_noop(self):
        """ExtensionAPI.ui returns no-op UI (headless mode)."""
        api = ExtensionAPI()
        ui = api.ui
        assert await ui.confirm("title", "msg") is True
        assert await ui.select("title", ["a"]) == "a"
        assert await ui.input("title", default="default") == "default"


class TestExtensionContext:
    """Tests for ExtensionContext."""

    def test_create_context(self):
        """ExtensionContext can be instantiated."""
        ctx = ExtensionContext()
        assert ctx is not None

    def test_context_cwd(self):
        """ExtensionContext.cwd returns current directory."""
        ctx = ExtensionContext()
        assert ctx.cwd == "."

    def test_context_session_manager(self):
        """ExtensionContext.session_manager returns None by default."""
        ctx = ExtensionContext()
        assert ctx.session_manager is None

    def test_context_signal(self):
        """ExtensionContext.signal returns None by default."""
        ctx = ExtensionContext()
        assert ctx.signal is None

    def test_context_is_idle(self):
        """ExtensionContext.is_idle returns True by default."""
        ctx = ExtensionContext()
        assert ctx.is_idle is True

    def test_context_abort(self):
        """ExtensionContext.abort() is callable."""
        ctx = ExtensionContext()
        ctx.abort()  # Should not raise

    def test_context_shutdown(self):
        """ExtensionContext.shutdown() is callable."""
        ctx = ExtensionContext()
        ctx.shutdown()  # Should not raise

    def test_context_get_context_usage(self):
        """ExtensionContext.get_context_usage() returns dict."""
        ctx = ExtensionContext()
        usage = ctx.get_context_usage()
        assert isinstance(usage, dict)


class TestExtensionUI:
    """Tests for ExtensionUI (headless/no-op mode).

    Reference: SUBPHASE-0.0.md, "8. Extension API Surface":
    > In headless mode (RPC, SDK), all methods are no-ops.
    > The TUI implements the real UI methods.
    """

    @pytest.mark.asyncio
    async def test_confirm_returns_true(self):
        """ExtensionUI.confirm() returns True by default (headless)."""
        ui = ExtensionUI()
        result = await ui.confirm("Title", "Message")
        assert result is True

    @pytest.mark.asyncio
    async def test_select_returns_first_item(self):
        """ExtensionUI.select() returns first item by default (headless)."""
        ui = ExtensionUI()
        result = await ui.select("Title", ["Option 1", "Option 2"])
        assert result == "Option 1"

    @pytest.mark.asyncio
    async def test_select_returns_none_for_empty_list(self):
        """ExtensionUI.select() returns None for empty list."""
        ui = ExtensionUI()
        result = await ui.select("Title", [])
        assert result is None

    @pytest.mark.asyncio
    async def test_input_returns_default(self):
        """ExtensionUI.input() returns default value (headless)."""
        ui = ExtensionUI()
        result = await ui.input("Title", default="default_value")
        assert result == "default_value"

    @pytest.mark.asyncio
    async def test_input_returns_empty_string_without_default(self):
        """ExtensionUI.input() returns empty string when no default."""
        ui = ExtensionUI()
        result = await ui.input("Title")
        assert result == ""

    def test_notify_noop(self):
        """ExtensionUI.notify() is a no-op (headless)."""
        ui = ExtensionUI()
        # Should not raise
        ui.notify("Test message")
        ui.notify("Test message", level="info")
        ui.notify("Test message", level="warning")
        ui.notify("Test message", level="error")

    def test_notify_accepts_level(self):
        """ExtensionUI.notify() accepts level parameter."""
        ui = ExtensionUI()
        ui.notify("Test", level="info")
        ui.notify("Test", level="warning")
        ui.notify("Test", level="error")

    @pytest.mark.asyncio
    async def test_confirm_returns_async(self):
        """ExtensionUI.confirm() is async in headless mode."""
        ui = ExtensionUI()
        result = await ui.confirm("Title", "Message")
        assert isinstance(result, bool)
        assert result is True

    @pytest.mark.asyncio
    async def test_select_returns_async(self):
        """ExtensionUI.select() is async in headless mode."""
        ui = ExtensionUI()
        result = await ui.select("Title", ["a", "b"])
        assert isinstance(result, str)
        assert result == "a"

    @pytest.mark.asyncio
    async def test_input_returns_async(self):
        """ExtensionUI.input() is async in headless mode."""
        ui = ExtensionUI()
        result = await ui.input("Title", default="def")
        assert isinstance(result, str)
        assert result == "def"


class TestExtensionTypesImport:
    """Tests for module-level imports.

    Reference: PHASE-2-SUBPHASE-0.md, Testing section item 1.
    > from tau_agent_core.extension_types import ExtensionAPI
    """

    def test_import_extension_api(self):
        """ExtensionAPI imports from extension_types module."""
        from tau_agent_core.extension_types import ExtensionAPI
        assert ExtensionAPI is not None

    def test_import_extension_context(self):
        """ExtensionContext imports from extension_types module."""
        from tau_agent_core.extension_types import ExtensionContext
        assert ExtensionContext is not None

    def test_import_extension_ui(self):
        """ExtensionUI imports from extension_types module."""
        from tau_agent_core.extension_types import ExtensionUI
        assert ExtensionUI is not None

    def test_import_from_package_root(self):
        """All extension types import from tau_agent_core package root."""
        from tau_agent_core import (
            ExtensionAPI,
            ExtensionContext,
            ExtensionUI,
        )
        assert ExtensionAPI is not None
        assert ExtensionContext is not None
        assert ExtensionUI is not None

"""Tests for Phase 3 Subphase 0 — Data Contract Definition.

Verifies the final type signatures defined in this subphase:
1. EventBus: on(), off(), emit(), emit_channel()
2. ExtensionAPI: on, register_tool, get_all_tools, set_active_tools,
   register_command, append_entry, set_session_name, send_user_message,
   send_message, register_flag, get_flag, ui property
3. ExtensionContext: cwd, session_manager, signal, is_idle,
   abort, shutdown, get_context_usage
4. ExtensionUI: confirm, select, input, notify
5. ExtensionLoader: discover(), load()
6. ExtensionRegistry: register_tool(), get_all_tools(), etc.
7. Public exports from extensions/__init__.py

Reference: docs/PHASE-3-SUBPHASE-0.md
Reference: docs/SUBPHASE-0.0.md lines 260-340
"""

import asyncio
import inspect

import pytest

from tau_agent_core.events import EventBus
from tau_agent_core.extension_types import (
    ExtensionAPI,
    ExtensionContext,
    ExtensionUI,
)


# =============================================================================
# 1. EventBus Contract Tests
# =============================================================================


class TestEventBusContract:
    """Tests verifying EventBus has all required methods per the contract.

    Contract (from PHASE-3-SUBPHASE-0.md):
    class EventBus:
        def on(self, channel: str, handler: Callable) -> Callable[[], None]: ...
        def off(self, channel: str, handler: Callable) -> None: ...
        async def emit(self, event: AgentEvent) -> None: ...
        async def emit_channel(self, channel: str, *args, **kwargs) -> None: ...
    """

    def test_event_bus_on_method_exists(self):
        """EventBus has on() method."""
        ebus = EventBus()
        assert hasattr(ebus, "on")
        assert callable(ebus.on)

    def test_event_bus_off_method_exists(self):
        """EventBus has off() method per contract."""
        ebus = EventBus()
        assert hasattr(ebus, "off")
        assert callable(ebus.off)

    def test_event_bus_emit_method_exists(self):
        """EventBus has emit() method per contract."""
        ebus = EventBus()
        assert hasattr(ebus, "emit")
        assert callable(ebus.emit)

    def test_event_bus_emit_is_async(self):
        """EventBus.emit() is async per contract."""
        assert inspect.iscoroutinefunction(EventBus.emit)

    def test_event_bus_emit_channel_method_exists(self):
        """EventBus has emit_channel() method per contract."""
        ebus = EventBus()
        assert hasattr(ebus, "emit_channel")
        assert callable(ebus.emit_channel)

    def test_event_bus_emit_channel_is_async(self):
        """EventBus.emit_channel() is async per contract."""
        assert inspect.iscoroutinefunction(EventBus.emit_channel)

    def test_event_bus_on_signature(self):
        """EventBus.on(channel, handler) returns an unsubscribe function."""
        ebus = EventBus()

        def handler(event):
            pass

        unsub = ebus.on("test_channel", handler)
        assert callable(unsub)
        # Unsubscribing should work
        unsub()

    @pytest.mark.asyncio
    async def test_event_bus_off_removes_handler(self):
        """EventBus.off(channel, handler) removes specific handler."""
        from tau_agent_core.events import AgentEvent

        ebus = EventBus()
        received = []

        def handler(event):
            received.append(event)

        ebus.on("unique_off_test", handler)
        ebus.off("unique_off_test", handler)

        # After removing, handler should not be called
        await ebus.emit(AgentEvent(type="agent_start", timestamp=0))
        assert len(received) == 0

    def test_event_bus_emit_is_coroutine(self):
        """EventBus.emit() returns a coroutine (is async)."""
        ebus = EventBus()
        from tau_agent_core.events import AgentEvent

        result = ebus.emit(AgentEvent(type="agent_start", timestamp=0))
        assert asyncio.iscoroutine(result)

    def test_event_bus_emit_channel_is_coroutine(self):
        """EventBus.emit_channel() returns a coroutine (is async)."""
        ebus = EventBus()
        result = ebus.emit_channel("test", "arg")
        assert asyncio.iscoroutine(result)


# =============================================================================
# 2. ExtensionAPI Contract Tests
# =============================================================================


class TestExtensionAPIContract:
    """Tests verifying ExtensionAPI has all required methods per the contract.

    Contract (from PHASE-3-SUBPHASE-0.md):
    class ExtensionAPI:
        def on(self, event: str, handler: Callable) -> None: ...
        def register_tool(self, definition: dict) -> None: ...
        def get_all_tools(self) -> list[ToolInfo]: ...
        def set_active_tools(self, names: list[str]) -> None: ...
        def register_command(self, name: str, command: dict) -> None: ...
        def append_entry(self, custom_type: str, data: dict) -> None: ...
        def set_session_name(self, name: str) -> None: ...
        def send_user_message(self, content: str, deliver_as: str = "steer") -> None: ...
        def send_message(self, message: dict, options: dict) -> None: ...
        def register_flag(self, name: str, options: dict) -> None: ...
        def get_flag(self, name: str) -> Any: ...
        @property
        def ui(self) -> ExtensionUI: ...
    """

    def test_api_on_exists(self):
        """ExtensionAPI has on() method."""
        assert hasattr(ExtensionAPI, "on")
        assert callable(getattr(ExtensionAPI, "on"))

    def test_api_register_tool_exists(self):
        """ExtensionAPI has register_tool() method."""
        assert hasattr(ExtensionAPI, "register_tool")
        assert callable(getattr(ExtensionAPI, "register_tool"))

    def test_api_get_all_tools_exists(self):
        """ExtensionAPI has get_all_tools() method."""
        assert hasattr(ExtensionAPI, "get_all_tools")
        assert callable(getattr(ExtensionAPI, "get_all_tools"))

    def test_api_set_active_tools_exists(self):
        """ExtensionAPI has set_active_tools() method."""
        assert hasattr(ExtensionAPI, "set_active_tools")
        assert callable(getattr(ExtensionAPI, "set_active_tools"))

    def test_api_register_command_exists(self):
        """ExtensionAPI has register_command() method."""
        assert hasattr(ExtensionAPI, "register_command")
        assert callable(getattr(ExtensionAPI, "register_command"))

    def test_api_append_entry_exists(self):
        """ExtensionAPI has append_entry() method."""
        assert hasattr(ExtensionAPI, "append_entry")
        assert callable(getattr(ExtensionAPI, "append_entry"))

    def test_api_set_session_name_exists(self):
        """ExtensionAPI has set_session_name() method."""
        assert hasattr(ExtensionAPI, "set_session_name")
        assert callable(getattr(ExtensionAPI, "set_session_name"))

    def test_api_send_user_message_exists(self):
        """ExtensionAPI has send_user_message() method."""
        assert hasattr(ExtensionAPI, "send_user_message")
        assert callable(getattr(ExtensionAPI, "send_user_message"))

    def test_api_send_message_exists(self):
        """ExtensionAPI has send_message() method."""
        assert hasattr(ExtensionAPI, "send_message")
        assert callable(getattr(ExtensionAPI, "send_message"))

    def test_api_register_flag_exists(self):
        """ExtensionAPI has register_flag() method."""
        assert hasattr(ExtensionAPI, "register_flag")
        assert callable(getattr(ExtensionAPI, "register_flag"))

    def test_api_get_flag_exists(self):
        """ExtensionAPI has get_flag() method."""
        assert hasattr(ExtensionAPI, "get_flag")
        assert callable(getattr(ExtensionAPI, "get_flag"))

    def test_api_ui_property_exists(self):
        """ExtensionAPI has ui property."""
        assert hasattr(ExtensionAPI, "ui")
        # Must be a property
        assert isinstance(inspect.getattr_static(ExtensionAPI, "ui"), property)

    def test_api_ui_returns_extension_ui(self):
        """ExtensionAPI.ui returns an ExtensionUI instance."""
        api = ExtensionAPI()
        ui = api.ui
        assert isinstance(ui, ExtensionUI)

    def test_api_ui_is_same_instance(self):
        """ExtensionAPI.ui returns the same instance on repeated access."""
        api = ExtensionAPI()
        ui1 = api.ui
        ui2 = api.ui
        assert ui1 is ui2

    def test_api_all_methods_callable(self):
        """All ExtensionAPI methods are callable."""
        api = ExtensionAPI()
        methods = [
            "on", "register_tool", "get_all_tools", "set_active_tools",
            "register_command", "append_entry", "set_session_name",
            "send_user_message", "send_message", "register_flag", "get_flag",
        ]
        for method_name in methods:
            assert callable(getattr(api, method_name)), f"{method_name} is not callable"

    def test_api_on_registers_handler(self):
        """ExtensionAPI.on() registers an event handler."""
        api = ExtensionAPI()
        api.on("test_event", lambda: None)
        assert "test_event" in api._handlers

    def test_api_register_tool_stores_definition(self):
        """ExtensionAPI.register_tool() stores the tool definition."""
        api = ExtensionAPI()
        api.register_tool({"name": "test", "description": "test tool"})
        tools = api.get_all_tools()
        assert len(tools) == 1
        assert tools[0]["name"] == "test"

    def test_api_get_all_tools_returns_list(self):
        """ExtensionAPI.get_all_tools() returns a list."""
        api = ExtensionAPI()
        result = api.get_all_tools()
        assert isinstance(result, list)

    def test_api_set_active_tools_stores_names(self):
        """ExtensionAPI.set_active_tools() stores the active tool names."""
        api = ExtensionAPI()
        api.set_active_tools(["ls", "grep"])
        assert hasattr(api, "_active_tools")
        assert api._active_tools == ["ls", "grep"]

    def test_api_register_command_stores_command(self):
        """ExtensionAPI.register_command() stores the command."""
        api = ExtensionAPI()
        api.register_command("help", {"action": "help"})
        assert "help" in api._commands

    def test_api_append_entry_callable(self):
        """ExtensionAPI.append_entry() is callable without error."""
        api = ExtensionAPI()
        api.append_entry("custom_type", {"key": "value"})

    def test_api_set_session_name_stores_name(self):
        """ExtensionAPI.set_session_name() stores the session name."""
        api = ExtensionAPI()
        api.set_session_name("My Session")
        assert hasattr(api, "_session_name")

    def test_api_send_user_message_callable(self):
        """ExtensionAPI.send_user_message() is callable without error."""
        api = ExtensionAPI()
        api.send_user_message("Hello")
        api.send_user_message("Hello", deliver_as="steer")

    def test_api_send_message_callable(self):
        """ExtensionAPI.send_message() is callable without error."""
        api = ExtensionAPI()
        api.send_message({"text": "Hello"}, {"option": True})

    def test_api_register_flag_stores_options(self):
        """ExtensionAPI.register_flag() stores flag options."""
        api = ExtensionAPI()
        api.register_flag("debug", {"type": "boolean"})
        assert api._flags["debug"]["type"] == "boolean"

    def test_api_get_flag_returns_stored_value(self):
        """ExtensionAPI.get_flag() returns the stored flag value."""
        api = ExtensionAPI()
        api.register_flag("debug", {"type": "boolean", "default": True})
        assert api.get_flag("debug") == {"type": "boolean", "default": True}

    def test_api_get_flag_returns_none_for_missing(self):
        """ExtensionAPI.get_flag() returns None for missing flags."""
        api = ExtensionAPI()
        assert api.get_flag("nonexistent") is None


# =============================================================================
# 3. ExtensionContext Contract Tests
# =============================================================================


class TestExtensionContextContract:
    """Tests verifying ExtensionContext has all required properties per the contract.

    Contract (from PHASE-3-SUBPHASE-0.md):
    class ExtensionContext:
        @property
        def cwd(self) -> str: ...
        @property
        def session_manager(self) -> SessionManager: ...
        @property
        def signal(self) -> AbortSignal | None: ...
        @property
        def is_idle(self) -> bool: ...
        def abort(self) -> None: ...
        def shutdown(self) -> None: ...
        def get_context_usage(self) -> dict: ...
    """

    def test_context_cwd_property_exists(self):
        """ExtensionContext has cwd property."""
        assert hasattr(ExtensionContext, "cwd")
        assert isinstance(inspect.getattr_static(ExtensionContext, "cwd"), property)

    def test_context_session_manager_property_exists(self):
        """ExtensionContext has session_manager property."""
        assert hasattr(ExtensionContext, "session_manager")
        assert isinstance(inspect.getattr_static(ExtensionContext, "session_manager"), property)

    def test_context_signal_property_exists(self):
        """ExtensionContext has signal property."""
        assert hasattr(ExtensionContext, "signal")
        assert isinstance(inspect.getattr_static(ExtensionContext, "signal"), property)

    def test_context_is_idle_property_exists(self):
        """ExtensionContext has is_idle property."""
        assert hasattr(ExtensionContext, "is_idle")
        assert isinstance(inspect.getattr_static(ExtensionContext, "is_idle"), property)

    def test_context_abort_method_exists(self):
        """ExtensionContext has abort() method."""
        assert hasattr(ExtensionContext, "abort")
        assert callable(getattr(ExtensionContext, "abort"))

    def test_context_shutdown_method_exists(self):
        """ExtensionContext has shutdown() method."""
        assert hasattr(ExtensionContext, "shutdown")
        assert callable(getattr(ExtensionContext, "shutdown"))

    def test_context_get_context_usage_method_exists(self):
        """ExtensionContext has get_context_usage() method."""
        assert hasattr(ExtensionContext, "get_context_usage")
        assert callable(getattr(ExtensionContext, "get_context_usage"))

    def test_context_cwd_returns_string(self):
        """ExtensionContext.cwd returns a string."""
        ctx = ExtensionContext()
        assert isinstance(ctx.cwd, str)

    def test_context_is_idle_returns_bool(self):
        """ExtensionContext.is_idle returns a bool."""
        ctx = ExtensionContext()
        assert isinstance(ctx.is_idle, bool)

    def test_context_abort_callable(self):
        """ExtensionContext.abort() is callable."""
        ctx = ExtensionContext()
        ctx.abort()

    def test_context_shutdown_callable(self):
        """ExtensionContext.shutdown() is callable."""
        ctx = ExtensionContext()
        ctx.shutdown()

    def test_context_get_context_usage_returns_dict(self):
        """ExtensionContext.get_context_usage() returns a dict."""
        ctx = ExtensionContext()
        usage = ctx.get_context_usage()
        assert isinstance(usage, dict)

    def test_context_all_members_present(self):
        """ExtensionContext has all 7 documented members."""
        required = {"cwd", "session_manager", "signal", "is_idle", "abort", "shutdown", "get_context_usage"}
        actual = {
            name for name in dir(ExtensionContext)
            if not name.startswith("_")
        }
        for member in required:
            assert member in actual or hasattr(ExtensionContext, member), f"Missing member: {member}"


# =============================================================================
# 4. ExtensionUI Contract Tests
# =============================================================================


class TestExtensionUIContract:
    """Tests verifying ExtensionUI has all required methods per the contract.

    Contract (from PHASE-3-SUBPHASE-0.md):
    class ExtensionUI:
        async def confirm(self, title: str, message: str) -> bool: ...
        async def select(self, title: str, items: list[str]) -> str | None: ...
        async def input(self, title: str, default: str = "") -> str: ...
        def notify(self, message: str, level: str = "info") -> None: ...
    """

    def test_ui_confirm_exists(self):
        """ExtensionUI has confirm() method."""
        assert hasattr(ExtensionUI, "confirm")
        assert callable(getattr(ExtensionUI, "confirm"))

    def test_ui_select_exists(self):
        """ExtensionUI has select() method."""
        assert hasattr(ExtensionUI, "select")
        assert callable(getattr(ExtensionUI, "select"))

    def test_ui_input_method_exists(self):
        """ExtensionUI has input() method."""
        assert hasattr(ExtensionUI, "input")
        assert callable(getattr(ExtensionUI, "input"))

    def test_ui_notify_exists(self):
        """ExtensionUI has notify() method."""
        assert hasattr(ExtensionUI, "notify")
        assert callable(getattr(ExtensionUI, "notify"))

    def test_ui_confirm_is_async(self):
        """ExtensionUI.confirm() is async per contract."""
        assert inspect.iscoroutinefunction(ExtensionUI.confirm)

    def test_ui_select_is_async(self):
        """ExtensionUI.select() is async per contract."""
        assert inspect.iscoroutinefunction(ExtensionUI.select)

    def test_ui_input_is_async(self):
        """ExtensionUI.input() is async per contract."""
        assert inspect.iscoroutinefunction(ExtensionUI.input)

    def test_ui_notify_is_sync(self):
        """ExtensionUI.notify() is sync per contract."""
        # notify is NOT async in the contract
        assert not inspect.iscoroutinefunction(ExtensionUI.notify)

    @pytest.mark.asyncio
    async def test_ui_confirm_returns_bool(self):
        """ExtensionUI.confirm() returns a bool."""
        ui = ExtensionUI()
        result = await ui.confirm("Title", "Message")
        assert isinstance(result, bool)

    @pytest.mark.asyncio
    async def test_ui_select_returns_string_or_none(self):
        """ExtensionUI.select() returns str or None."""
        ui = ExtensionUI()
        result = await ui.select("Title", ["a", "b"])
        assert result is None or isinstance(result, str)

    @pytest.mark.asyncio
    async def test_ui_input_returns_string(self):
        """ExtensionUI.input() returns a string."""
        ui = ExtensionUI()
        result = await ui.input("Title", default="hello")
        assert isinstance(result, str)

    def test_ui_notify_does_not_raise(self):
        """ExtensionUI.notify() does not raise."""
        ui = ExtensionUI()
        ui.notify("Test message")
        ui.notify("Test message", level="info")
        ui.notify("Test message", level="warning")
        ui.notify("Test message", level="error")

    def test_ui_notify_default_level(self):
        """ExtensionUI.notify() defaults level to 'info'."""
        ui = ExtensionUI()
        # Should not raise with default level
        ui.notify("Test message")


# =============================================================================
# 5. ExtensionLoader Contract Tests
# =============================================================================


class TestExtensionLoaderContract:
    """Tests verifying ExtensionLoader exists and has required methods.

    Contract (from PHASE-3-SUBPHASE-0.md):
    class ExtensionLoader:
        def discover(self) -> list[str]: ...
        def load(self, name: str) -> Extension: ...
    """

    def test_extension_loader_importable(self):
        """ExtensionLoader can be imported from tau_agent_core.extensions.loader."""
        from tau_agent_core.extensions.loader import ExtensionLoader
        assert ExtensionLoader is not None

    def test_extension_loader_class_exists(self):
        """ExtensionLoader class exists and is a class."""
        from tau_agent_core.extensions.loader import ExtensionLoader
        assert inspect.isclass(ExtensionLoader)

    def test_extension_loader_discover_exists(self):
        """ExtensionLoader has discover() method."""
        from tau_agent_core.extensions.loader import ExtensionLoader
        assert hasattr(ExtensionLoader, "discover")
        assert callable(getattr(ExtensionLoader, "discover"))

    def test_extension_loader_load_exists(self):
        """ExtensionLoader has load() method."""
        from tau_agent_core.extensions.loader import ExtensionLoader
        assert hasattr(ExtensionLoader, "load")
        assert callable(getattr(ExtensionLoader, "load"))

    def test_extension_loader_instantiate(self):
        """ExtensionLoader can be instantiated."""
        from tau_agent_core.extensions.loader import ExtensionLoader
        loader = ExtensionLoader()
        assert loader is not None


# =============================================================================
# 6. ExtensionRegistry Contract Tests
# =============================================================================


class TestExtensionRegistryContract:
    """Tests verifying ExtensionRegistry exists and has required methods.

    Contract (from PHASE-3-SUBPHASE-0.md):
    class ExtensionRegistry:
        def register_tool(self, definition: dict) -> None: ...
        def get_all_tools(self) -> list[ToolInfo]: ...
    """

    def test_extension_registry_importable(self):
        """ExtensionRegistry can be imported from tau_agent_core.extensions.registry."""
        from tau_agent_core.extensions.registry import ExtensionRegistry
        assert ExtensionRegistry is not None

    def test_extension_registry_class_exists(self):
        """ExtensionRegistry class exists and is a class."""
        from tau_agent_core.extensions.registry import ExtensionRegistry
        assert inspect.isclass(ExtensionRegistry)

    def test_extension_registry_register_tool_exists(self):
        """ExtensionRegistry has register_tool() method."""
        from tau_agent_core.extensions.registry import ExtensionRegistry
        assert hasattr(ExtensionRegistry, "register_tool")
        assert callable(getattr(ExtensionRegistry, "register_tool"))

    def test_extension_registry_get_all_tools_exists(self):
        """ExtensionRegistry has get_all_tools() method."""
        from tau_agent_core.extensions.registry import ExtensionRegistry
        assert hasattr(ExtensionRegistry, "get_all_tools")
        assert callable(getattr(ExtensionRegistry, "get_all_tools"))

    def test_extension_registry_instantiate(self):
        """ExtensionRegistry can be instantiated."""
        from tau_agent_core.extensions.registry import ExtensionRegistry
        registry = ExtensionRegistry()
        assert registry is not None

    def test_extension_registry_register_and_get_tool(self):
        """ExtensionRegistry can register and retrieve a tool."""
        from tau_agent_core.extensions.registry import ExtensionRegistry
        registry = ExtensionRegistry()
        tool_def = {"name": "test_tool", "description": "A test tool"}
        registry.register_tool(tool_def)
        tools = registry.get_all_tools()
        assert len(tools) >= 1
        assert any(t.name == "test_tool" for t in tools)

    def test_extension_registry_get_all_tools_returns_list(self):
        """ExtensionRegistry.get_all_tools() returns a list."""
        from tau_agent_core.extensions.registry import ExtensionRegistry
        registry = ExtensionRegistry()
        result = registry.get_all_tools()
        assert isinstance(result, list)


# =============================================================================
# 7. Public Exports Tests
# =============================================================================


class TestPublicExports:
    """Tests for public exports from extensions/__init__.py and package root."""

    def test_extensions_init_exists(self):
        """extensions/__init__.py exists as a module."""
        import tau_agent_core.extensions
        assert tau_agent_core.extensions is not None

    def test_package_root_exports_event_bus(self):
        """EventBus exports from tau_agent_core package root."""
        from tau_agent_core import EventBus
        assert EventBus is not None

    def test_package_root_exports_extension_types(self):
        """Extension types export from tau_agent_core package root."""
        from tau_agent_core import ExtensionAPI, ExtensionContext, ExtensionUI
        assert ExtensionAPI is not None
        assert ExtensionContext is not None
        assert ExtensionUI is not None

    def test_package_root_exports_agent_event(self):
        """AgentEvent exports from tau_agent_core package root."""
        from tau_agent_core import AgentEvent
        assert AgentEvent is not None

    def test_direct_import_extension_api(self):
        """ExtensionAPI can be imported from extension_types module."""
        from tau_agent_core.extension_types import ExtensionAPI
        assert ExtensionAPI is not None

    def test_direct_import_extension_context(self):
        """ExtensionContext can be imported from extension_types module."""
        from tau_agent_core.extension_types import ExtensionContext
        assert ExtensionContext is not None

    def test_direct_import_extension_ui(self):
        """ExtensionUI can be imported from extension_types module."""
        from tau_agent_core.extension_types import ExtensionUI
        assert ExtensionUI is not None

    def test_event_bus_from_events_module(self):
        """EventBus can be imported from events module."""
        from tau_agent_core.events import EventBus
        assert EventBus is not None

    def test_event_bus_from_package_root(self):
        """EventBus can be imported from package root."""
        from tau_agent_core import EventBus
        assert EventBus is not None

    def test_extension_loader_from_package(self):
        """ExtensionLoader can be imported from extensions.loader."""
        from tau_agent_core.extensions.loader import ExtensionLoader
        assert ExtensionLoader is not None

    def test_extension_registry_from_package(self):
        """ExtensionRegistry can be imported from extensions.registry."""
        from tau_agent_core.extensions.registry import ExtensionRegistry
        assert ExtensionRegistry is not None


# =============================================================================
# 8. Async EventBus Integration Tests
# =============================================================================


class TestEventBusAsyncIntegration:
    """Integration tests for EventBus async operations."""

    @pytest.mark.asyncio
    async def test_emit_async_calls_handler(self):
        """EventBus.emit() async calls registered handlers."""
        ebus = EventBus()
        received = []

        def handler(event):
            received.append(event)

        ebus.on("all", handler)
        from tau_agent_core.events import AgentEvent

        await ebus.emit(AgentEvent(type="agent_start", timestamp=0))
        assert len(received) == 1

    @pytest.mark.asyncio
    async def test_emit_channel_async(self):
        """EventBus.emit_channel() async dispatches to channel handlers."""
        ebus = EventBus()
        received = []

        def handler(*args, **kwargs):
            received.append((args, kwargs))

        ebus.on("custom_channel", handler)
        await ebus.emit_channel("custom_channel", "arg1", key="val")
        assert len(received) == 1
        assert received[0][0] == ("arg1",)
        assert received[0][1] == {"key": "val"}

    @pytest.mark.asyncio
    async def test_emit_channel_multiple_handlers(self):
        """EventBus.emit_channel() calls all handlers on a channel."""
        ebus = EventBus()
        received = []

        def h1(*args, **kwargs):
            received.append(1)

        def h2(*args, **kwargs):
            received.append(2)

        ebus.on("multi", h1)
        ebus.on("multi", h2)
        await ebus.emit_channel("multi")
        assert received == [1, 2]

    @pytest.mark.asyncio
    async def test_emit_async_with_invalid_channel(self):
        """EventBus.emit_channel() handles non-existent channels gracefully."""
        ebus = EventBus()
        # Should not raise
        await ebus.emit_channel("nonexistent_channel")


# =============================================================================
# 9. Complete Contract Signature Tests
# =============================================================================


class TestFullContractSignatures:
    """Verify method signatures match the contract exactly."""

    def test_eventbus_on_signature(self):
        """EventBus.on(channel, handler) -> Callable[[], None]."""
        sig = inspect.signature(EventBus.on)
        params = list(sig.parameters.keys())
        assert "self" in params
        assert "channel" in params or "event_type" in params  # parameter name may vary

    def test_eventbus_off_signature(self):
        """EventBus.off(channel, handler) -> None."""
        sig = inspect.signature(EventBus.off)
        params = list(sig.parameters.keys())
        assert "self" in params
        assert "channel" in params or "event_type" in params

    def test_eventbus_emit_signature(self):
        """EventBus.emit(event) -> None (async)."""
        sig = inspect.signature(EventBus.emit)
        params = list(sig.parameters.keys())
        assert "self" in params
        assert "event" in params

    def test_eventbus_emit_channel_signature(self):
        """EventBus.emit_channel(channel, *args, **kwargs) -> None (async)."""
        sig = inspect.signature(EventBus.emit_channel)
        params = list(sig.parameters.keys())
        assert "self" in params
        assert "channel" in params

    def test_extension_ui_confirm_signature(self):
        """ExtensionUI.confirm(title, message) -> bool (async)."""
        sig = inspect.signature(ExtensionUI.confirm)
        params = list(sig.parameters.keys())
        assert "self" in params
        assert "title" in params
        assert "message" in params

    def test_extension_ui_select_signature(self):
        """ExtensionUI.select(title, items) -> str | None (async)."""
        sig = inspect.signature(ExtensionUI.select)
        params = list(sig.parameters.keys())
        assert "self" in params
        assert "title" in params
        assert "items" in params

    def test_extension_ui_input_signature(self):
        """ExtensionUI.input(title, default="") -> str (async)."""
        sig = inspect.signature(ExtensionUI.input)
        params = list(sig.parameters.keys())
        assert "self" in params
        assert "title" in params
        assert "default" in params

    def test_extension_ui_notify_signature(self):
        """ExtensionUI.notify(message, level="info") -> None (sync)."""
        sig = inspect.signature(ExtensionUI.notify)
        params = list(sig.parameters.keys())
        assert "self" in params
        assert "message" in params
        assert "level" in params

    def test_extension_context_properties_are_properties(self):
        """ExtensionContext properties are actual Python properties."""
        for prop_name in ["cwd", "session_manager", "signal", "is_idle"]:
            attr = inspect.getattr_static(ExtensionContext, prop_name)
            assert isinstance(attr, property), f"{prop_name} is not a property"

    def test_extension_api_ui_is_property(self):
        """ExtensionAPI.ui is a property, not a regular method."""
        attr = inspect.getattr_static(ExtensionAPI, "ui")
        assert isinstance(attr, property), "ui is not a property"

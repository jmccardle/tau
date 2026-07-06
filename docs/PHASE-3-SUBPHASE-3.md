# Phase 3 Subphase 3 — Extension API Surface

> **Topic**: Implement the ExtensionAPI, ExtensionContext, and ExtensionUI classes that extensions use.

## Scope

This subphase implements the **public API surface** that extension modules interact with. Extensions import `ExtensionAPI` from `tau_agent_core` and receive an instance when their `register()` function is called.

The key challenge is making the API work in both TUI and headless modes:
- In TUI mode: all UI methods (confirm, select, input, notify) work
- In headless mode: UI methods are no-ops or raise NotImplementedError
- The same extension should work in both modes

## Reference

- `SUBPHASE-3-SUBPHASE-0.md`: extension API contracts
- `SUBPHASE-0.0.md` lines 300-340: extension API details
- `docs/extensions.md` lines 100-250: extension examples
- `docs/tau-agent-core.md` lines 500-600: extension types

## Implementation Outline

### `tau_agent_core/extensions/types.py`

```python
import asyncio
from typing import Any, Callable

class ExtensionUI:
    """User interaction methods (TUI only).

    In headless mode, all methods are no-ops:
    - confirm() returns True (auto-approve)
    - select() returns the first item (or None if empty)
    - input() returns the default value
    - notify() is a no-op (prints to stderr)
    """

    def __init__(self, mode: Literal["tui", "headless"] = "headless"):
        self._mode = mode
        self._tui_delegate = None  # set by TUI when running in TUI mode

    async def confirm(self, title: str, message: str) -> bool:
        if self._mode == "tui" and self._tui_delegate:
            return await self._tui_delegate.confirm(title, message)
        return True  # headless: auto-approve

    async def select(self, title: str, items: list[str]) -> str | None:
        if self._mode == "tui" and self._tui_delegate:
            return await self._tui_delegate.select(title, items)
        return items[0] if items else None  # headless: pick first

    async def input(self, title: str, default: str = "") -> str:
        if self._mode == "tui" and self._tui_delegate:
            return await self._tui_delegate.input(title, default)
        return default  # headless: use default

    def notify(self, message: str, level: str = "info") -> None:
        if self._mode == "tui" and self._tui_delegate:
            self._tui_delegate.notify(message, level)
        else:
            import sys
            print(f"[τ] {level}: {message}", file=sys.stderr)


class ExtensionContext:
    """Context passed to extension event handlers and tools."""

    def __init__(
        self,
        cwd: str,
        session_manager: Any,  # SessionManager (avoid circular import)
        signal: Any | None,    # AbortSignal
        is_idle: bool = True,
    ):
        self._cwd = cwd
        self._session_manager = session_manager
        self._signal = signal
        self._is_idle = is_idle
        self._ui = ExtensionUI(mode="headless")

    @property
    def cwd(self) -> str:
        return self._cwd

    @property
    def session_manager(self) -> Any:
        return self._session_manager

    @property
    def signal(self) -> Any | None:
        return self._signal

    @property
    def is_idle(self) -> bool:
        return self._is_idle

    def abort(self) -> None:
        if self._signal:
            self._signal.abort()

    def shutdown(self) -> None:
        """Request graceful shutdown."""
        if hasattr(self._session_manager, "shutdown"):
            self._session_manager.shutdown()

    def get_context_usage(self) -> dict:
        """Get current context window usage."""
        # Would need to calculate from current messages
        return {"total_tokens": 0}

    def set_ui_delegate(self, delegate: Any) -> None:
        """Set the TUI delegate for UI methods."""
        self._ui._mode = "tui"
        self._ui._tui_delegate = delegate
        # Also set on self._ui which is accessible via ExtensionAPI.ui
```

### ExtensionAPI

```python
class ExtensionAPI:
    """Public API exposed to all extension modules."""

    def __init__(
        self,
        registry: Any,  # ExtensionRegistry
        event_bus: Any,  # EventBus
        context: ExtensionContext,
        session: Any,    # AgentSession (for messaging)
    ):
        self._registry = registry
        self._event_bus = event_bus
        self._context = context
        self._session = session
        self._flags: dict[str, dict] = {}

    def on(self, event: str, handler: Callable) -> None:
        """Subscribe to an event."""
        if event == "all":
            self._event_bus.on("all", handler)
        else:
            self._event_bus.on(event, handler)

    def register_tool(self, definition: dict) -> None:
        """Register a tool callable by the LLM."""
        definition["_source"] = "extension"
        self._registry.register_tool(definition)

    def get_all_tools(self) -> list[Any]:
        """Get all registered tools."""
        return self._registry.get_all_tools()

    def set_active_tools(self, names: list[str]) -> None:
        """Enable/disable tools by name."""
        self._registry.set_active_tools(names)

    def register_command(self, name: str, command: dict) -> None:
        """Register a slash command."""
        self._registry.register_command(name, command)

    def append_entry(self, custom_type: str, data: dict) -> None:
        """Persist extension state."""
        self._registry.append_entry(custom_type, data)

    def set_session_name(self, name: str) -> None:
        """Set the session display name."""
        if hasattr(self._session, "_session_name"):
            self._session._session_name = name

    def send_user_message(self, content: str, deliver_as: str = "steer") -> None:
        """Send a user message to the agent."""
        if hasattr(self._session, "_queue_message"):
            self._session._queue_message(content, deliver_as=deliver_as)

    def send_message(self, message: dict, options: dict) -> None:
        """Send a custom message into the session."""
        if hasattr(self._session, "_append_custom_message"):
            self._session._append_custom_message(message, options)

    def register_flag(self, name: str, options: dict) -> None:
        """Register a CLI flag."""
        self._flags[name] = options
        self._registry.register_flag(name, options)

    def get_flag(self, name: str) -> Any:
        """Get the value of a CLI flag."""
        return self._flags.get(name, {}).get("value")

    @property
    def ui(self) -> ExtensionUI:
        """UI methods (TUI-only, no-ops in headless mode)."""
        return self._context._ui
```

### Key Design Decisions

1. **Headless-safe UI**: `ExtensionUI` returns sensible defaults in headless mode. This means extensions written for TUI can run headlessly without modification.
2. **Lazy binding**: `ExtensionAPI` doesn't import τ-agent-core internals. It uses duck typing (e.g., `hasattr(self._session, "_queue_message")`).
3. **Registry delegation**: Tool/command/flag registration goes through the registry. The API is a thin wrapper.
4. **Context injection**: `ExtensionContext` is created per-session and passed to extensions via the API.

## Done Criteria

- `ExtensionAPI` has all methods listed in `SUBPHASE-3-SUBPHASE-0.md`
- `ExtensionContext` has all properties listed in `SUBPHASE-3-SUBPHASE-0.md`
- `ExtensionUI` has all methods listed in `SUBPHASE-3-SUBPHASE-0.md`
- In headless mode: `confirm()` returns True, `select()` returns first item, `input()` returns default, `notify()` prints to stderr
- In TUI mode: methods delegate to the TUI delegate
- `ExtensionAPI.on()` subscribes to the event bus
- `ExtensionAPI.register_tool()` registers with the registry
- `ExtensionAPI.append_entry()` persists extension state
- Same extension works in both TUI and headless modes

## Testing Strategy

### Test 1: ExtensionAPI method existence

```python
async def test_extension_api_methods():
    api = ExtensionAPI(
        registry=ExtensionRegistry(),
        event_bus=EventBus(),
        context=ExtensionContext(cwd="/tmp", session_manager=None, signal=None),
        session=None,
    )
    assert hasattr(api, "on")
    assert hasattr(api, "register_tool")
    assert hasattr(api, "get_all_tools")
    assert hasattr(api, "set_active_tools")
    assert hasattr(api, "register_command")
    assert hasattr(api, "append_entry")
    assert hasattr(api, "set_session_name")
    assert hasattr(api, "send_user_message")
    assert hasattr(api, "send_message")
    assert hasattr(api, "register_flag")
    assert hasattr(api, "get_flag")
    assert hasattr(api, "ui")
```

### Test 2: Headless UI defaults

```python
async def test_headless_ui_defaults():
    ui = ExtensionUI(mode="headless")
    assert await ui.confirm("title", "msg") is True
    assert await ui.select("title", ["a", "b"]) == "a"
    assert await ui.input("title", "default") == "default"
    # notify() should not raise
    ui.notify("test message", "info")
```

### Test 3: TUI UI delegation

```python
async def test_tui_delegation():
    class MockUI:
        async def confirm(self, title, message):
            return False
        async def select(self, title, items):
            return "selected"
        async def input(self, title, default):
            return "typed"
        def notify(self, message, level):
            pass

    ui = ExtensionUI(mode="headless")
    ui.set_ui_delegate(MockUI())
    ui._mode = "tui"
    ui._tui_delegate = MockUI()

    assert await ui.confirm("title", "msg") is False
    assert await ui.select("title", ["a", "b"]) == "selected"
    assert await ui.input("title", "default") == "typed"
```

### Test 4: Extension registration

```python
async def test_extension_tool_registration():
    registry = ExtensionRegistry()
    bus = EventBus()
    ctx = ExtensionContext(cwd="/tmp", session_manager=None, signal=None)
    api = ExtensionAPI(registry=registry, event_bus=bus, context=ctx, session=None)

    api.register_tool({
        "name": "test_tool",
        "description": "test desc",
        "parameters": {"type": "object"},
        "execute": lambda *a: None,
    })
    tools = registry.get_all_tools()
    assert len(tools) == 1
    assert tools[0].name == "test_tool"
    assert tools[0].source == "extension"
```

### Test 5: Extension event subscription

```python
async def test_extension_event_subscription():
    bus = EventBus()
    ctx = ExtensionContext(cwd="/tmp", session_manager=None, signal=None)
    api = ExtensionAPI(
        registry=ExtensionRegistry(),
        event_bus=bus,
        context=ctx,
        session=None,
    )

    received = []
    api.on("agent_start", lambda e: received.append(e))

    await bus.emit(AgentEvent(type="agent_start"))
    assert len(received) == 1
```

### Test 6: Extension entry persistence

```python
async def test_extension_entry_persistence():
    registry = ExtensionRegistry()
    ctx = ExtensionContext(cwd="/tmp", session_manager=None, signal=None)
    api = ExtensionAPI(registry=registry, event_bus=EventBus(), context=ctx, session=None)

    api.append_entry("counter", {"value": 1})
    api.append_entry("counter", {"value": 2})
    entries = registry.get_entries()
    assert len(entries) == 2
    assert entries[0]["custom_type"] == "counter"
    assert entries[0]["data"]["value"] == 1
```

### Test 7: ExtensionContext properties

```python
async def test_extension_context_properties():
    ctx = ExtensionContext(
        cwd="/tmp/test",
        session_manager=None,
        signal=None,
        is_idle=False,
    )
    assert ctx.cwd == "/tmp/test"
    assert ctx.session_manager is None
    assert ctx.signal is None
    assert ctx.is_idle is False
```

### Test 8: ExtensionContext abort

```python
async def test_extension_context_abort():
    signal = AbortSignal()
    ctx = ExtensionContext(cwd="/tmp", session_manager=None, signal=signal)
    assert not signal.is_aborted()
    ctx.abort()
    assert signal.is_aborted()
```

## Success Signal

All 8 test categories pass. The extension API is the complete surface that extension modules use. It works in both TUI and headless modes. Tool registration, event subscription, and entry persistence all work correctly. The same extension code works everywhere.

# Phase 3 Subphase 0 — Data Contract Definition

> **Topic**: Finalize the extension system types, event bus interface, and loader/registry contracts.

## Scope

This subphase locks the types that the extension system defines and that other phases consume. These types must be defined before any implementation begins because Phase 2 (agent loop) needs to import them, and Phase 4 (TUI) needs to know the extension API surface.

## Done Criteria

The following files contain the final type signatures:

1. **`tau_agent_core/events.py`**: `EventBus` class with `on()`, `emit()`, `off()` methods
2. **`tau_agent_core/extensions/types.py`**: `ExtensionAPI`, `ExtensionContext`, `ExtensionUI`
3. **`tau_agent_core/extensions/loader.py`**: `ExtensionLoader` class with `discover()`, `load()` methods
4. **`tau_agent_core/extensions/registry.py`**: `ExtensionRegistry` class with `register_tool()`, `get_all_tools()`, etc.
5. **`tau_agent_core/extensions/__init__.py`**: Public exports

### EventBus Contract

```python
class EventBus:
    def on(self, channel: str, handler: Callable) -> Callable[[], None]:
        """Subscribe to channel. Returns unsubscribe function."""

    def off(self, channel: str, handler: Callable) -> None:
        """Remove specific handler from channel."""

    async def emit(self, event: AgentEvent) -> None:
        """Emit event to all handlers on matching channels."""

    async def emit_channel(self, channel: str, *args, **kwargs) -> None:
        """Emit to specific channel."""
```

### ExtensionAPI Contract

```python
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
```

### ExtensionContext Contract

```python
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
```

### ExtensionUI Contract

```python
class ExtensionUI:
    async def confirm(self, title: str, message: str) -> bool: ...
    async def select(self, title: str, items: list[str]) -> str | None: ...
    async def input(self, title: str, default: str = "") -> str: ...
    def notify(self, message: str, level: str = "info") -> None: ...
```

## Reference

- `SUBPHASE-0.0.md` lines 260-340: extension API contracts
- `docs/extensions.md` lines 60-180: extension design
- `docs/tau-agent-core.md` lines 450-600: extension types

## Testing

```python
# 1. All types import
from tau_agent_core.events import EventBus
from tau_agent_core.extension_types import ExtensionAPI, ExtensionContext, ExtensionUI
from tau_agent_core.extensions.loader import ExtensionLoader
from tau_agent_core.extensions.registry import ExtensionRegistry

# 2. EventBus has the right methods
import inspect
ebus = EventBus()
assert hasattr(ebus, "on")
assert hasattr(ebus, "off")
assert hasattr(ebus, "emit")
assert hasattr(ebus, "emit_channel")

# 3. ExtensionAPI has all required methods
assert hasattr(ExtensionAPI, "on")
assert hasattr(ExtensionAPI, "register_tool")
assert hasattr(ExtensionAPI, "get_all_tools")
assert hasattr(ExtensionAPI, "set_active_tools")
assert hasattr(ExtensionAPI, "register_command")
assert hasattr(ExtensionAPI, "append_entry")
assert hasattr(ExtensionAPI, "set_session_name")
assert hasattr(ExtensionAPI, "send_user_message")
assert hasattr(ExtensionAPI, "send_message")
assert hasattr(ExtensionAPI, "register_flag")
assert hasattr(ExtensionAPI, "get_flag")
assert hasattr(ExtensionAPI, "ui")

# 4. ExtensionContext has all required properties
assert hasattr(ExtensionContext, "cwd")
assert hasattr(ExtensionContext, "session_manager")
assert hasattr(ExtensionContext, "signal")
assert hasattr(ExtensionContext, "is_idle")
assert hasattr(ExtensionContext, "abort")
assert hasattr(ExtensionContext, "shutdown")
assert hasattr(ExtensionContext, "get_context_usage")

# 5. ExtensionUI has all required methods
assert hasattr(ExtensionUI, "confirm")
assert hasattr(ExtensionUI, "select")
assert hasattr(ExtensionUI, "input")
assert hasattr(ExtensionUI, "notify")
```

## Success Signal

All types import. All methods and properties match the contract in `SUBPHASE-0.0.md`. This is the final contract before Phase 3 implementation begins.

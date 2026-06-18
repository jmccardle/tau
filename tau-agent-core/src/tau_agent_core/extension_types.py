"""τ-agent-core extension_types: Extension API surface for extensions.

Reference: SUBPHASE-0.0.md, "8. Extension API Surface" section.

Components:
- ExtensionAPI: Public API exposed to extension modules
- ExtensionContext: Context passed to extension event handlers
- ExtensionUI: User interaction methods (TUI only, no-op in headless)

Constraint: Extensions must not import τ-agent-core internals.
The ui property is a no-op in headless mode (RPC, SDK).
"""

from __future__ import annotations

from typing import Any, Callable


class ExtensionUI:
    """User interaction methods (TUI only, no-op in headless mode).

    Reference: SUBPHASE-0.0.md, "8. Extension API Surface" section.

    In headless mode (RPC, SDK), all methods are no-ops.
    The TUI implements the real UI methods.
    """

    async def confirm(self, title: str, message: str) -> bool:
        """Show a confirmation dialog. Returns user's choice."""
        return True

    async def select(self, title: str, items: list[str]) -> str | None:
        """Show a selection dialog. Returns selected item or None."""
        return items[0] if items else None

    async def input(self, title: str, default: str = "") -> str:
        """Show an input dialog. Returns user input or default."""
        return default

    def notify(self, message: str, level: str = "info") -> None:
        """Show a notification."""
        pass


class ExtensionAPI:
    """Public API exposed to extension modules.

    Reference: SUBPHASE-0.0.md, "8. Extension API Surface" section.

    This is the ONLY API extension modules use. Extensions must not
    import τ-agent-core internals.
    """

    def __init__(self) -> None:
        self._handlers: dict[str, list[Callable]] = {}
        self._tools: list[dict] = []
        self._commands: dict[str, dict] = {}
        self._flags: dict[str, Any] = {}

    def on(self, event: str, handler: Callable) -> None:
        """Register an event handler."""
        if event not in self._handlers:
            self._handlers[event] = []
        self._handlers[event].append(handler)

    def register_tool(self, definition: dict) -> None:
        """Register a tool definition."""
        self._tools.append(definition)

    def get_all_tools(self) -> list[dict]:
        """Get all registered tools."""
        return self._tools

    def set_active_tools(self, names: list[str]) -> None:
        """Set which tools are active."""
        self._active_tools = names

    def register_command(self, name: str, command: dict) -> None:
        """Register a CLI command."""
        self._commands[name] = command

    def append_entry(self, custom_type: str, data: dict) -> None:
        """Append a custom entry to the session."""
        pass

    def set_session_name(self, name: str) -> None:
        """Set the session name."""
        self._session_name = name

    def send_user_message(self, content: str, deliver_as: str = "steer") -> None:
        """Send a user message into the agent loop."""
        pass

    def send_message(self, message: dict, options: dict) -> None:
        """Send a message directly."""
        pass

    def register_flag(self, name: str, options: dict) -> None:
        """Register a command-line flag."""
        self._flags[name] = options

    def get_flag(self, name: str) -> Any:
        """Get a flag value."""
        return self._flags.get(name)

    @property
    def ui(self) -> ExtensionUI:
        """Return the ExtensionUI instance.

        In headless mode, this returns a no-op UI.
        In TUI mode, this returns the real UI instance.
        """
        return ExtensionUI()


class ExtensionContext:
    """Context passed to extension event handlers and tools.

    Reference: SUBPHASE-0.0.md, "8. Extension API Surface" section.
    """

    @property
    def cwd(self) -> str:
        """Current working directory."""
        return "."

    @property
    def session_manager(self) -> Any:
        """The SessionManager instance."""
        return None

    @property
    def signal(self) -> Any | None:
        """AbortSignal for this context."""
        return None

    @property
    def is_idle(self) -> bool:
        """Whether the agent is idle."""
        return True

    def abort(self) -> None:
        """Abort the current operation."""
        pass

    def shutdown(self) -> None:
        """Shutdown the agent."""
        pass

    def get_context_usage(self) -> dict:
        """Get context usage information."""
        return {}

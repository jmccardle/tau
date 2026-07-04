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

from typing import TYPE_CHECKING, Any, Callable, Literal

if TYPE_CHECKING:
    from tau_agent_core.events import EventBus
    from tau_agent_core.extensions.registry import ExtensionRegistry


class ExtensionUI:
    """User interaction methods (TUI only, no-op in headless mode).

    Reference: SUBPHASE-0.0.md, "8. Extension API Surface" section.

    In headless mode (RPC, SDK), all methods are no-ops:
    - confirm() returns True (auto-approve)
    - select() returns the first item (or None if empty)
    - input() returns the default value
    - notify() prints to stderr

    In TUI mode, methods delegate to a TUI delegate.

    Attributes:
        _mode: "tui" or "headless"
        _tui_delegate: TUI delegate object (set via set_ui_delegate())
    """

    def __init__(self, mode: Literal["tui", "headless"] = "headless") -> None:
        """Initialize ExtensionUI.

        Args:
            mode: Either 'tui' or 'headless'. Defaults to 'headless'.
        """
        self._mode: Literal["tui", "headless"] = mode
        self._tui_delegate: Any | None = None

    async def confirm(self, title: str, message: str) -> bool:
        """Show a confirmation dialog. Returns user's choice.

        In TUI mode, delegates to the TUI delegate.
        In headless mode, returns True (auto-approve).
        """
        if self._mode == "tui" and self._tui_delegate:
            confirmed: bool = await self._tui_delegate.confirm(title, message)
            return confirmed
        return True  # headless: auto-approve

    async def select(self, title: str, items: list[str]) -> str | None:
        """Show a selection dialog. Returns selected item or None.

        In TUI mode, delegates to the TUI delegate.
        In headless mode, returns the first item (or None if empty).
        """
        if self._mode == "tui" and self._tui_delegate:
            selected: str | None = await self._tui_delegate.select(title, items)
            return selected
        return items[0] if items else None  # headless: pick first

    async def input(self, title: str, default: str = "") -> str:
        """Show an input dialog. Returns user input or default.

        In TUI mode, delegates to the TUI delegate.
        In headless mode, returns the default value.
        """
        if self._mode == "tui" and self._tui_delegate:
            entered: str = await self._tui_delegate.input(title, default)
            return entered
        return default  # headless: use default

    def notify(self, message: str, level: str = "info") -> None:
        """Show a notification.

        In TUI mode, delegates to the TUI delegate.
        In headless mode, prints to stderr.
        """
        if self._mode == "tui" and self._tui_delegate:
            self._tui_delegate.notify(message, level)
        else:
            import sys

            print(f"[τ] {level}: {message}", file=sys.stderr)


class ExtensionContext:
    """Context passed to extension event handlers and tools.

    Reference: SUBPHASE-0.0.md, "8. Extension API Surface" section.

    Attributes:
        _cwd: Current working directory.
        _session_manager: SessionManager instance (or None).
        _signal: AbortSignal for this context (or None).
        _is_idle: Whether the agent is idle.
        _ui: ExtensionUI instance.
    """

    def __init__(
        self,
        cwd: str = ".",
        session_manager: Any | None = None,
        signal: Any | None = None,
        is_idle: bool = True,
    ) -> None:
        """Initialize ExtensionContext.

        Args:
            cwd: Current working directory. Defaults to ".".
            session_manager: SessionManager instance. Defaults to None.
            signal: AbortSignal for this context. Defaults to None.
            is_idle: Whether the agent is idle. Defaults to True.
        """
        self._cwd = cwd
        self._session_manager = session_manager
        self._signal = signal
        self._is_idle = is_idle
        self._ui = ExtensionUI(mode="headless")

    @property
    def cwd(self) -> str:
        """Current working directory."""
        return self._cwd

    @property
    def session_manager(self) -> Any:
        """The SessionManager instance."""
        return self._session_manager

    @property
    def signal(self) -> Any | None:
        """AbortSignal for this context."""
        return self._signal

    @property
    def is_idle(self) -> bool:
        """Whether the agent is idle."""
        return self._is_idle

    def abort(self) -> None:
        """Abort the current operation by calling signal.abort() if available."""
        if self._signal:
            self._signal.abort()

    def shutdown(self) -> None:
        """Shutdown the agent by calling session_manager.shutdown() if available."""
        if self._session_manager is not None and hasattr(self._session_manager, "shutdown"):
            self._session_manager.shutdown()

    def get_context_usage(self) -> dict:
        """Get context usage information."""
        return {"total_tokens": 0}

    def set_ui_delegate(self, delegate: Any) -> None:
        """Set the TUI delegate for UI methods.

        This enables TUI mode on the internal ExtensionUI,
        setting the delegate for all UI interactions.

        Args:
            delegate: TUI delegate object implementing confirm/select/input/notify.
        """
        self._ui._mode = "tui"
        self._ui._tui_delegate = delegate


class ExtensionAPI:
    """Public API exposed to extension modules.

    Reference: SUBPHASE-0.0.md, "8. Extension API Surface" section.

    This is the ONLY API extension modules use. Extensions must not
    import τ-agent-core internals.

    Attributes:
        _registry: ExtensionRegistry for tool/command/flag management.
        _event_bus: EventBus for event subscription.
        _context: ExtensionContext with session state.
        _session: AgentSession for messaging.
        _flags: Dict of registered CLI flags.
    """

    def __init__(
        self,
        registry: ExtensionRegistry | None = None,
        event_bus: EventBus | None = None,
        context: ExtensionContext | None = None,
        session: Any = None,
    ) -> None:
        """Initialize ExtensionAPI.

        Args:
            registry: ExtensionRegistry for tool/command/flag management.
            event_bus: EventBus for event subscription.
            context: ExtensionContext with session state.
            session: AgentSession for messaging.
        """
        # Lazy initialization for backward compatibility
        if registry is None:
            from tau_agent_core.extensions.registry import ExtensionRegistry

            registry = ExtensionRegistry()
        if event_bus is None:
            from tau_agent_core.events import EventBus

            event_bus = EventBus()
        if context is None:
            context = ExtensionContext()

        self._registry = registry
        self._event_bus = event_bus
        self._context = context
        self._session = session
        self._flags: dict[str, dict[str, Any]] = {}

    def on(self, event: str, handler: Callable) -> Callable[[], None]:
        """Subscribe to an event on the live session event bus.

        Args:
            event: Event type (e.g., 'agent_start', 'all').
            handler: Callable that receives an AgentEvent.

        Returns:
            An unsubscribe function.
        """
        return self._event_bus.on(event, handler)

    def register_tool(self, definition: dict) -> None:
        """Register a tool callable by the LLM (pi ``ToolDefinition`` shape).

        Mirrors pi's ``registerTool(tool: ToolDefinition)``
        (coding-agent/src/core/extensions/types.ts:433). The definition is a
        plain dict — NOT a Pydantic/TypeBox model — carrying:

        - ``name`` (str): tool name used in LLM tool calls.
        - ``description`` (str): description sent to the LLM.
        - ``parameters`` (dict): JSON-schema dict for argument validation.
        - ``execute`` (callable): ``execute(tool_call_id, params, signal,
          on_update, ctx)`` returning an ``AgentToolResult``-shaped value
          (may be sync or async). ``ctx`` is the bound ``ExtensionContext``.

        Optional keys: ``label`` (defaults to ``name`` for UI), ``prompt_snippet``,
        ``prompt_guidelines``, ``execution_mode`` ("sequential"/"parallel").

        Adds ``_source: "extension"`` and registers with the session-owned
        registry; the resolved tool is merged into the loop's tools next turn.

        Raises:
            ValueError: if a required key is missing.
            TypeError: if ``parameters`` is not a dict or ``execute`` is not callable.
        """
        for key in ("name", "description", "parameters", "execute"):
            if key not in definition:
                raise ValueError(f"register_tool: missing required key '{key}'")
        if not isinstance(definition["parameters"], dict):
            raise TypeError("register_tool: 'parameters' must be a JSON-schema dict")
        if not callable(definition["execute"]):
            raise TypeError("register_tool: 'execute' must be callable")

        definition = dict(definition)  # don't mutate caller's dict
        definition["_source"] = "extension"
        self._registry.register_tool(definition)

    def get_all_tools(self) -> list[Any]:
        """Get all registered tools.

        Returns:
            List of tool info from the registry.
        """
        return self._registry.get_all_tools()

    def set_active_tools(self, names: list[str]) -> None:
        """Enable/disable tools by name (forwards to the registry)."""
        self._registry.set_active_tools(names)

    def register_command(self, name: str, command: dict) -> None:
        """Register a slash command (forwards to the registry)."""
        self._registry.register_command(name, command)

    def append_entry(self, custom_type: str, data: dict) -> None:
        """Persist extension state through the registry."""
        self._registry.append_entry(custom_type, data)

    def set_session_name(self, name: str) -> None:
        """Set the session display name on the bound session."""
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
        """Register a CLI flag.

        Also registers the flag with the registry.

        Args:
            name: Flag name.
            options: Flag options dict (e.g., {'type': 'boolean'}).
        """
        self._flags[name] = options
        self._registry.register_flag(name, options)

    def get_flag(self, name: str) -> Any:
        """Get the value of a CLI flag.

        Args:
            name: Flag name.

        Returns:
            The flag value, or None if not registered.
        """
        return self._flags.get(name, {}).get("value")

    @property
    def ui(self) -> ExtensionUI:
        """UI methods (TUI-only, no-ops in headless mode).

        Returns:
            The ExtensionUI instance from the context.
        """
        return self._context._ui

    @property
    def context(self) -> ExtensionContext:
        """The ExtensionContext for this API."""
        return self._context

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

from tau_agent_core.compaction import estimate_context_tokens

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
        # The live AgentSession, bound by ExtensionAPI so get_context_usage() can
        # read real messages + model.context_window. None until bound.
        self._session: Any | None = None

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

    def get_context_usage(self) -> dict[str, Any] | None:
        """Return context usage for the active model (pi ``ContextUsage`` shape).

        Faithful port of pi's ``getContextUsage`` (agent-session.ts:2975 →
        ``ContextUsage`` at types.ts:281-287): returns
        ``{tokens, context_window, percent}`` where ``tokens`` is the estimated
        context-token count from ``estimate_context_tokens`` — the SAME estimate
        that drives auto-compaction (agent_session.py:523) — and ``percent`` is
        ``tokens / context_window * 100``.

        Returns ``None`` when the model has no positive ``context_window`` (pi
        returns ``undefined``); that is a genuine "unknown", not a fabricated
        zero.

        Raises:
            RuntimeError: if no session is bound — there is nothing to measure.
                Replaces the old fictional ``{"total_tokens": 0}`` stub
                (Fail-Early: raise rather than fabricate).
        """
        session = self._session
        if session is None:
            raise RuntimeError("get_context_usage: no session bound to ExtensionContext")
        model = getattr(session, "_model", None)
        context_window = int(getattr(model, "context_window", 0) or 0)
        if context_window <= 0:
            return None
        tokens = estimate_context_tokens(session.messages).tokens
        percent = (tokens / context_window) * 100
        return {"tokens": tokens, "context_window": context_window, "percent": percent}

    # ------------------------------------------------------------------
    # Session-control op surface (E3-ctx / step S19)
    #
    # These expose the LANDED session-tree substrate on the base handler
    # context so agent tools can drive it (plan decision 2; the E2 gatekeeper
    # veto is the safety). Each delegates to the one authoritative session log
    # the bound ``AgentSession`` persists through, so the mutation is visible on
    # both the TUI live path and headless. pi keeps fork/navigate command-only
    # (types.ts:354-373); τ places them on the base context (decision 2 / §7
    # E3-b). Fail-Early: an unbound session, or an op the concrete log cannot
    # satisfy (e.g. exporting an in-memory log), RAISES rather than no-ops.
    # ------------------------------------------------------------------

    def _require_session(self) -> Any:
        """The bound ``AgentSession``, or raise (Fail-Early, no silent no-op)."""
        if self._session is None:
            raise RuntimeError("session-control op: no session bound to ExtensionContext")
        return self._session

    async def compact(self, custom_instructions: str | None = None) -> Any:
        """Compact the active conversation now (delegates to ``AgentSession.compact``).

        Runs the full append-only compaction pipeline on the bound session's log
        (``agent_session.py`` ``compact``): build the active-path entries, summarize
        the compacted prefix via the LLM, and APPEND a compaction entry so the
        prefix drops out of future context at read time. Returns the
        ``CompactionResult`` (or ``None`` when there is nothing to compact).

        This is the immediate variant; the turn-end-deferred variant lands in S20.

        Args:
            custom_instructions: Optional extra focus for the summary.
        """
        return await self._require_session().compact(custom_instructions=custom_instructions)

    def entries(self) -> list[dict[str, Any]]:
        """The bound session log's raw, append-only entries (all kinds).

        Thin pass-through to ``SessionLog.entries()`` — the same entry list a
        ``ConversationTree`` folds into context. Read-only: a copy per the log's
        contract, so mutating the returned list does not touch the log.
        """
        entries: list[dict[str, Any]] = self._require_session().session_log.entries()
        return entries

    async def summarize_branch(
        self, from_entry: str, custom_instructions: str | None = None
    ) -> list[dict[str, Any]]:
        """Summarize the subtree at ``from_entry`` and splice it onto the active path.

        Ports the summarize arm of ``TauBackend.navigate_tree`` (backends.py:246)
        onto the bound session's own log: extract the branch text
        (``ConversationTree.subtree_text(from_entry)``), summarize it via the module
        ``summarize_branch`` (session_manager.py:705 — already raise-based on a
        failed/empty summary, Fail-Early), then APPEND a ``branch_summary`` entry
        parented at ``from_entry`` (``SessionLog.append_branch_summary``). The
        abandoned children drop out of context via the ``parentId`` walk.

        Returns the re-rendered active-path messages (``ConversationTree.context_for``).
        """
        from tau_agent_core.conversation_tree import ConversationTree
        from tau_agent_core.session_manager import summarize_branch as _summarize_branch

        session = self._require_session()
        log = session.session_log
        old_leaf = log.cursor
        branch_text = ConversationTree(log.entries(), old_leaf).subtree_text(from_entry)
        summary = await _summarize_branch(
            branch_text,
            session._model,
            api_key=session._api_key,
            custom_instructions=custom_instructions,
        )
        log.append_branch_summary(summary, from_entry)
        return ConversationTree(log.entries(), log.cursor).context_for()

    async def navigate(
        self,
        target_id: str | None,
        summarize: bool = False,
        custom_instructions: str | None = None,
    ) -> list[dict[str, Any]]:
        """Move the bound session's cursor to ``target_id`` and return the new context.

        Ports ``TauBackend.navigate_tree`` (backends.py:246) onto the bound
        session's own log. ``summarize=False`` APPENDs a ``navigate`` entry (zero
        LLM calls); the abandoned branch drops out of context via the ``parentId``
        walk but stays on disk. ``summarize=True`` delegates to
        :meth:`summarize_branch` (append a ``branch_summary`` at the branch point).
        A ``target_id`` already at the cursor is a no-op (pi ``navigateTree:2716``).

        Returns the re-rendered active-path messages (``ConversationTree.context_for``).
        """
        from tau_agent_core.conversation_tree import ConversationTree

        session = self._require_session()
        log = session.session_log
        if target_id == log.cursor:
            return ConversationTree(log.entries(), log.cursor).context_for()
        if summarize:
            if target_id is None:
                raise ValueError("navigate(summarize=True) requires a target_id to summarize")
            return await self.summarize_branch(target_id, custom_instructions=custom_instructions)
        log.append_navigate(target_id)
        return ConversationTree(log.entries(), log.cursor).context_for()

    async def fork(
        self, entry_id: str | None = None, mode: Literal["in_place", "export"] = "in_place"
    ) -> Any:
        """Fork the conversation — one op, two modes (plan §7 decision E3-b).

        - ``mode="in_place"`` (default): branch WITHIN the one session log by
          APPENDing a ``navigate`` to ``entry_id`` (``entry_id=None`` → pre-root),
          so the next turn appends a sibling branch off that point. Returns the
          re-rendered active-path messages (``ConversationTree.context_for``). This
          is the ``navigate+append`` in-place fork.
        - ``mode="export"``: copy the session into a NEW file via ``Session.fork``
          (session_store.py:347; the source log is never touched), optionally
          positioning the new file's cursor at ``entry_id``. Returns the new
          session file path as a string.

        Fail-Early: export requires a concrete file-backed ``Session`` log — an
        in-memory SDK log cannot be exported to a file and RAISES rather than
        fabricating one.
        """
        from tau_agent_core.conversation_tree import ConversationTree

        session = self._require_session()
        log = session.session_log
        if mode == "in_place":
            log.append_navigate(entry_id)
            return ConversationTree(log.entries(), log.cursor).context_for()
        if mode == "export":
            fork_classmethod = getattr(type(log), "fork", None)
            if fork_classmethod is None:
                raise RuntimeError(
                    "fork(mode='export'): the bound session log is not file-backed and "
                    "cannot be exported to a new file"
                )
            # Fork into the source's own cwd partition (pi keeps a fork in the
            # same session dir); fall back to the context cwd for a log that does
            # not expose one.
            cwd = getattr(log, "cwd", None) or self._cwd
            forked = fork_classmethod(log, cwd)
            if entry_id is not None:
                forked.append_navigate(entry_id)
            return str(forked.path)
        raise ValueError(f"fork: unknown mode {mode!r} (expected 'in_place' or 'export')")

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
        # Bind the live session onto the context so ctx.get_context_usage()
        # (delegated to the session in pi) reads real messages + model window.
        self._context._session = session
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

    def send_user_message(self, content: str, deliver_as: str = "followUp") -> None:
        """Queue a user message for the agent (pi ``sendUserMessage``).

        ``deliver_as`` selects the delivery mode. The parameter stays a plain
        ``str`` so future modes stay extensible (decision 5), but the two modes
        the queue supports are validated here:

        - ``"followUp"`` (default): drains at the end of the current ``prompt()``
          and re-enters within the same call.
        - ``"nextTurn"``: queued for the next ``prompt()``.

        Raises:
            ValueError: if ``deliver_as`` is not ``"followUp"`` or ``"nextTurn"``.
            RuntimeError: if the session has no message queue yet — the real
                ``_queue_message`` lands in E3-ctx. Fail-Early: raise rather than
                silently drop the message (the old ``hasattr`` no-op).
        """
        if deliver_as not in ("followUp", "nextTurn"):
            raise ValueError(
                "send_user_message: deliver_as must be 'followUp' or 'nextTurn', "
                f"got {deliver_as!r}"
            )
        if not hasattr(self._session, "_queue_message"):
            raise RuntimeError(
                "send_user_message: session message queue not available yet "
                "(the queue lands in E3-ctx)"
            )
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

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
    from tau_agent_core.extensions.runner import ExtensionHandlers

#: Hook names that existed through E2 but were removed. ``api.on`` rejects them
#: with a Fail-Early raise (E5 §3.2 / S30) rather than binding them silently to the
#: notify ``EventBus`` (a dead no-op, since nothing emits these channels).
_RETIRED_HOOKS: frozenset[str] = frozenset({"context"})

#: Headless dialog policy (E7 §3 / S48 — anchor G9, decision D-E6-2).
#:
#: Maps each BLOCKING dialog method (``confirm``/``select``/``input``) to the set
#: of answer tokens that EXPLICITLY restore an auto-answer when it fires headless
#: (no TUI delegate). With no policy entry for a method the dialog RAISES
#: (:class:`HeadlessDialogError`) instead of silently auto-resolving — the old
#: silent ``confirm→True`` / ``select→first`` was a fallback that could
#: auto-approve a permission gate, exactly the anti-pattern the standing rule
#: forbids. A user opts back into the auto-answer per method via
#: ``--ui-defaults confirm=yes,select=first`` or a config.json ``"ui_defaults"``
#: block. Tokens are lower-cased before matching.
HEADLESS_DIALOG_ANSWERS: dict[str, frozenset[str]] = {
    "confirm": frozenset({"yes", "no", "true", "false"}),
    "select": frozenset({"first"}),
    "input": frozenset({"default"}),
}

#: Confirm tokens that resolve to ``True`` (the rest of ``confirm`` → ``False``).
_CONFIRM_TRUE_TOKENS: frozenset[str] = frozenset({"yes", "true"})


class HeadlessDialogError(RuntimeError):
    """A UI dialog opened in headless mode with no explicit ``--ui-defaults`` policy.

    Raised by :meth:`ExtensionUI.confirm` / :meth:`ExtensionUI.select` /
    :meth:`ExtensionUI.input` when there is no TUI delegate AND the corresponding
    method has no headless-answer policy (E7 §3 / S48). A headless run cannot ask
    a human, and silently auto-answering would fabricate consent for a gate — so
    Fail-Early: raise, naming the ``--ui-defaults`` opt-in that restores an
    explicit auto-answer.
    """


class ExtensionUI:
    """User interaction methods (TUI delegate, or a headless policy).

    Reference: SUBPHASE-0.0.md, "8. Extension API Surface"; E7 §3 / S48.

    In TUI mode the blocking dialogs (``confirm``/``select``/``input``) delegate
    to a TUI delegate that asks a real human. In headless mode there is no human,
    so each blocking dialog obeys the headless-answer POLICY set via
    :meth:`set_headless_defaults` (from ``--ui-defaults`` / config.json):

    - a method WITH a policy entry returns the explicitly-configured answer
      (``confirm`` → ``True``/``False``; ``select`` → first item; ``input`` →
      default);
    - a method WITHOUT one RAISES :class:`HeadlessDialogError` (S48 / D-E6-2).

    The pre-S48 behaviour auto-answered every headless dialog (``confirm→True``,
    ``select→first``, ``input→default``) with no way to opt out — a silent
    auto-approve of whatever the dialog was gating. Raising by default makes the
    auto-answer an EXPLICIT choice instead of a hidden fallback.

    ``notify`` is non-blocking (no answer to fabricate): it prints to stderr
    headless and paints on the delegate in TUI mode — unchanged.

    Attributes:
        _mode: "tui" or "headless"
        _tui_delegate: TUI delegate object (set via set_ui_delegate())
        _headless_policy: validated ``{method: token}`` headless-answer map
    """

    def __init__(
        self,
        mode: Literal["tui", "headless"] = "headless",
        headless_policy: dict[str, str] | None = None,
    ) -> None:
        """Initialize ExtensionUI.

        Args:
            mode: Either 'tui' or 'headless'. Defaults to 'headless'.
            headless_policy: Optional ``{method: token}`` headless-answer map
                (validated via :meth:`set_headless_defaults`). Defaults to no
                policy → headless dialogs raise (S48).
        """
        self._mode: Literal["tui", "headless"] = mode
        self._tui_delegate: Any | None = None
        self._headless_policy: dict[str, str] = {}
        if headless_policy:
            self.set_headless_defaults(headless_policy)

    def set_headless_defaults(self, policy: dict[str, str]) -> None:
        """Set (replace) the headless-answer policy, validating every entry (S48).

        ``policy`` maps a dialog method to its answer token; keys must be in
        :data:`HEADLESS_DIALOG_ANSWERS` and each token must be one of that method's
        allowed answers (case-insensitive). Fail-Early: an unknown method or token
        RAISES :class:`ValueError` rather than being silently ignored, so a
        typo in ``--ui-defaults`` / config surfaces instead of leaving a dialog
        unexpectedly raising at runtime.
        """
        validated: dict[str, str] = {}
        for method, token in policy.items():
            allowed = HEADLESS_DIALOG_ANSWERS.get(method)
            if allowed is None:
                raise ValueError(
                    f"ui-defaults: unknown dialog {method!r} "
                    f"(expected one of {sorted(HEADLESS_DIALOG_ANSWERS)})"
                )
            token_l = str(token).strip().lower()
            if token_l not in allowed:
                raise ValueError(
                    f"ui-defaults: {method}={token!r} is not a valid answer "
                    f"(expected one of {sorted(allowed)})"
                )
            validated[method] = token_l
        self._headless_policy = validated

    def _headless_token(self, method: str, detail: str) -> str:
        """The configured headless answer token for ``method``, or raise (S48).

        Fail-Early: with no policy entry there is no human to ask and no explicit
        auto-answer, so raise :class:`HeadlessDialogError` naming the opt-in.
        """
        token = self._headless_policy.get(method)
        if token is None:
            raise HeadlessDialogError(
                f"ui.{method}({detail!r}) was called in headless mode with no "
                f"--ui-defaults policy for {method!r}. A headless run cannot ask a "
                "human, and auto-answering would silently resolve the dialog. Pass "
                f"--ui-defaults {method}=<answer> (allowed: "
                f"{sorted(HEADLESS_DIALOG_ANSWERS[method])}) or set config.json "
                '"ui_defaults", or run in the TUI.'
            )
        return token

    async def confirm(self, title: str, message: str) -> bool:
        """Show a confirmation dialog. Returns user's choice.

        In TUI mode, delegates to the TUI delegate. In headless mode, returns the
        policy answer (``confirm=yes/true`` → ``True``, ``confirm=no/false`` →
        ``False``) or raises :class:`HeadlessDialogError` when no policy is set.
        """
        if self._mode == "tui" and self._tui_delegate:
            confirmed: bool = await self._tui_delegate.confirm(title, message)
            return confirmed
        return self._headless_token("confirm", title) in _CONFIRM_TRUE_TOKENS

    async def select(self, title: str, items: list[str]) -> str | None:
        """Show a selection dialog. Returns selected item or None.

        In TUI mode, delegates to the TUI delegate. In headless mode, ``select=first``
        returns the first item (or None if empty); no policy raises
        :class:`HeadlessDialogError`.
        """
        if self._mode == "tui" and self._tui_delegate:
            selected: str | None = await self._tui_delegate.select(title, items)
            return selected
        self._headless_token("select", title)  # raises if no policy; only "first" is valid
        return items[0] if items else None

    async def input(self, title: str, default: str = "") -> str:
        """Show an input dialog. Returns user input or default.

        In TUI mode, delegates to the TUI delegate. In headless mode, ``input=default``
        returns the default value; no policy raises :class:`HeadlessDialogError`.
        """
        if self._mode == "tui" and self._tui_delegate:
            entered: str = await self._tui_delegate.input(title, default)
            return entered
        self._headless_token("input", title)  # raises if no policy; only "default" is valid
        return default

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
    # Model + usage access (E6 §2 / S45 — anchor G14)
    #
    # Public accessors so extensions stop reaching the private ``_session._model``
    # or hand-parsing ``event.message`` for usage. Each delegates to the bound
    # ``AgentSession`` (the authoritative holder of the live model + last usage) and
    # Fail-Early raises when no session is bound — there is nothing to read/switch.
    # ------------------------------------------------------------------

    def get_model(self) -> dict[str, Any]:
        """The active model as ``{id, provider, context_window}`` (S45).

        Delegates to :meth:`AgentSession.get_model`. Mirrors pi's ``ctx.model``
        (types.ts:311) but as the three-field projection an extension needs, so it
        never has to reach the private ``ctx._session._model``.

        Raises:
            RuntimeError: if no session is bound (Fail-Early — no model to read).
        """
        model: dict[str, Any] = self._require_session().get_model()
        return model

    def set_model(self, name: str) -> dict[str, Any]:
        """Switch the active model by NAME, effective next turn (S45).

        Delegates to :meth:`AgentSession.set_model` (pi ``setModel`` parity, adapted
        to τ's name-based resolver). Returns the new :meth:`get_model` projection.

        Raises:
            RuntimeError: if no session is bound, or the session has no model
                resolver bound (both Fail-Early — no registry to resolve ``name``).
            Whatever the resolver raises for an unknown ``name`` propagates unchanged.
        """
        model: dict[str, Any] = self._require_session().set_model(name)
        return model

    def get_usage(self) -> dict[str, Any] | None:
        """The most recent completion's token usage, or ``None`` (S45).

        The public per-completion usage accessor (anchor G14): delegates to
        :meth:`AgentSession.get_usage`. Returns a copy of the last completion's
        ``usage`` dict, or ``None`` when no completion has landed yet. Read this from
        a ``message_end`` handler instead of pulling ``event.message["usage"]``.

        Raises:
            RuntimeError: if no session is bound (Fail-Early — nothing to measure).
        """
        usage: dict[str, Any] | None = self._require_session().get_usage()
        return usage

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

    async def compact(self, custom_instructions: str | None = None, defer: bool = False) -> Any:
        """Compact the active conversation (delegates to ``AgentSession.compact``).

        Runs the full append-only compaction pipeline on the bound session's log
        (``agent_session.py`` ``compact``): build the active-path entries, summarize
        the compacted prefix via the LLM, and APPEND a compaction entry so the
        prefix drops out of future context at read time. Returns the
        ``CompactionResult`` (or ``None`` when there is nothing to compact).

        Two variants (S20 / decision 3):

        - ``defer=False`` (default): the IMMEDIATE variant — compacts now and
          returns the ``CompactionResult``.
        - ``defer=True``: the TURN-END-DEFERRED variant — a tool calling this
          mid-turn cannot compact under the live agent loop, so the intent is
          RECORDED and applied exactly once at the tail of ``prompt()`` (the same
          site as auto-compaction). Returns ``None`` immediately; the tool then
          returns its own normal result.

        Args:
            custom_instructions: Optional extra focus for the summary.
            defer: When True, record the intent for the end-of-prompt drain
                instead of compacting now.
        """
        session = self._require_session()
        if defer:
            session._defer_compact(custom_instructions=custom_instructions)
            return None
        return await session.compact(custom_instructions=custom_instructions)

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
        self,
        entry_id: str | None = None,
        mode: Literal["in_place", "export"] = "in_place",
        defer: bool = False,
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

        ``defer=True`` (S20 / decision 3): a tool calling this mid-turn cannot
        re-parent the log under the live agent loop, so the intent is RECORDED and
        applied exactly once at the tail of ``prompt()``. Returns ``None``
        immediately; the tool then returns its own normal result.

        Fail-Early: export requires a concrete file-backed ``Session`` log — an
        in-memory SDK log cannot be exported to a file and RAISES rather than
        fabricating one.
        """
        from tau_agent_core.conversation_tree import ConversationTree

        session = self._require_session()
        if defer:
            session._defer_fork(entry_id=entry_id, mode=mode)
            return None
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

    def set_headless_ui_defaults(self, policy: dict[str, str]) -> None:
        """Set the headless dialog-answer policy on the shared UI (E7 §3 / S48).

        Delegates to :meth:`ExtensionUI.set_headless_defaults`; the frontends call
        this (via :meth:`AgentSession.set_headless_ui_defaults`) with the resolved
        ``--ui-defaults`` / config policy so a headless dialog auto-answers only
        when the user opted in. Validation (unknown method/token) raises
        ``ValueError`` — the caller surfaces it as a clean CLI error.
        """
        self._ui.set_headless_defaults(policy)


class ExtensionAPI:
    """Public API exposed to extension modules.

    Reference: SUBPHASE-0.0.md, "8. Extension API Surface" section.

    This is the ONLY API extension modules use. Extensions must not
    import τ-agent-core internals.

    Attributes:
        _registry: ExtensionRegistry for tool/command management.
        _event_bus: EventBus for event subscription.
        _context: ExtensionContext with session state.
        _session: AgentSession for messaging.
    """

    def __init__(
        self,
        registry: ExtensionRegistry | None = None,
        event_bus: EventBus | None = None,
        context: ExtensionContext | None = None,
        session: Any = None,
        hook_handlers: ExtensionHandlers | None = None,
        config: dict[str, Any] | None = None,
    ) -> None:
        """Initialize ExtensionAPI.

        Args:
            registry: ExtensionRegistry for tool/command/flag management.
            event_bus: EventBus for event subscription.
            context: ExtensionContext with session state.
            session: AgentSession for messaging.
            hook_handlers: This extension's OWN ``ExtensionHandlers`` bucket in the
                session ``ExtensionRunner`` (load order preserved). ``api.on()``
                routes the four MUTATING hooks (``tool_call`` / ``tool_result`` /
                ``before_agent_start`` / ``context``) here — the dispatch surface
                the loop's hook call-sites gate on. Left ``None`` for an api that is
                not bound to a runner bucket; registering a hook on such an api then
                RAISES (Fail-Early) rather than silently no-op'ing (S24).
            config: This extension's OWN per-extension config slice (E6 §2 / S40).
                Sourced from ``~/.tau/config.json`` ``"extensions": {"<name>": {…}}``
                keyed by the extension's file stem, with per-run
                ``--ext-config <name>.<key>=<value>`` overrides applied on top. The
                session slices the right entry per extension in
                ``AgentSession._bind_extension_api`` and passes it here; ``None`` →
                an empty dict (an unconfigured extension reads ``{}``, never a
                fabricated value — Fail-Early leaves defaulting to the extension).
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
        # This extension's own hook-handler bucket in the session's
        # ExtensionRunner (None when the api is not bound to a runner).
        self._hook_handlers = hook_handlers
        # This extension's own per-extension config slice (S40). Copied so a later
        # mutation of the source map (or another extension's slice) can't bleed in;
        # nested values are shared by reference (config is read-only by contract).
        self._config: dict[str, Any] = dict(config or {})
        # Bind the live session onto the context so ctx.get_context_usage()
        # (delegated to the session in pi) reads real messages + model window.
        self._context._session = session

    def on(self, event: str, handler: Callable) -> Callable[[], None]:
        """Subscribe to an event — routed by KIND (S24 bridge).

        The five MUTATING hooks (``ExtensionRunner.HOOK_EVENTS``: ``tool_call`` /
        ``tool_result`` / ``before_agent_start`` / ``input`` / ``turn_end``) AND the
        two notify-grade session-lifecycle hooks (``ExtensionRunner.LIFECYCLE_EVENTS``:
        ``session_start`` / ``session_shutdown``, S41) are dispatched by the
        session's separate ``ExtensionRunner``, whose call-sites gate on
        ``has_handlers(event)``. Those registrations must land in THIS extension's
        runner bucket (``self._hook_handlers``), not on the notify ``EventBus`` —
        otherwise they are a silent no-op in a real session (the bug S24 closes),
        and the lifecycle hooks in particular route through the runner precisely so
        their handler errors are SURFACED (S44) instead of swallowed like the bus.
        Every other (notify) event keeps going to the ``EventBus``.

        ``turn_end`` (S43) is the mutating variant: ``api.on("turn_end", …)`` now
        routes to the runner, where a handler may return ``{message}`` for a durable
        append or return nothing to observe. The notify-grade ``turn_end``
        ``AgentEvent`` on the ``EventBus`` is UNCHANGED — pure observers still reach
        it via ``api.on("all", …)`` or :meth:`AgentSession.subscribe`.

        The retired ``context`` hook (E5 §3.2 / S30) is rejected UP FRONT: it was
        removed from ``HOOK_EVENTS``, so left unguarded it would fall through to the
        notify ``EventBus`` and bind silently to a channel nothing ever emits — a
        dead no-op. Fail-Early: raise an unknown-hook error naming the durable
        replacement instead.

        Args:
            event: Event type (e.g., 'agent_start', 'tool_call', 'all').
            handler: Callable that receives the event (an ``AgentEvent`` for notify
                events; a ``(event_dict, ctx)`` pair for hook events).

        Returns:
            An unsubscribe function.

        Raises:
            RuntimeError: registering the retired ``context`` hook (removed in E5
                §3.2 / S30), or registering a mutating hook on an api that was never
                bound to a runner bucket (``hook_handlers is None``). Fail-Early — a
                hook with nowhere to dispatch is a construction bug, not a no-op.
        """
        from tau_agent_core.extensions.runner import ExtensionRunner

        if event in _RETIRED_HOOKS:
            raise RuntimeError(
                f"api.on({event!r}): the {event!r} hook was removed in E5 §3.2 / S30. "
                "Under the durable-hook invariant the model's input is exactly the "
                "system prompt + the linear active path — there is no per-call "
                "message-list transform. Achieve the same effect with a durable node "
                "edit: patch the triggering `tool_result` via api.on('tool_result', …) "
                "and/or inject a pre-first-call message via "
                "api.on('before_agent_start', …)."
            )

        if event in ExtensionRunner.HOOK_EVENTS or event in ExtensionRunner.LIFECYCLE_EVENTS:
            if self._hook_handlers is None:
                raise RuntimeError(
                    f"api.on({event!r}): this ExtensionAPI is not bound to an "
                    "ExtensionRunner bucket, so the hook could never fire. "
                    "Obtain the api from AgentSession's extension load path "
                    "(each factory is handed a bucket-bound api)."
                )
            hook_bucket = self._hook_handlers
            hook_bucket.on(event, handler)

            def unsubscribe_hook() -> None:
                handlers = hook_bucket.handlers.get(event)
                if handlers is not None:
                    try:
                        handlers.remove(handler)
                    except ValueError:
                        pass

            return unsubscribe_hook

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
        # Attribute the tool to THIS extension for the /extensions surface (E5 §5 /
        # S34). The registry stores tools globally (by name); the per-extension
        # runner bucket is the only place that records which extension owns it.
        if self._hook_handlers is not None:
            self._hook_handlers.tools.append(definition["name"])

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
        # Attribute the command to THIS extension for the /extensions surface (E5
        # §5 / S34); the registry stores commands globally, with no per-extension
        # source (see register_tool).
        if self._hook_handlers is not None:
            self._hook_handlers.commands.append(name)

    def append_entry(self, custom_type: str, data: dict) -> None:
        """Persist durable, NON-message extension state onto the session tree (E6 §2 / S39).

        Appends a ``{customType, data}`` node of its own tree entry KIND
        (``customEntry``) to the authoritative session log via
        ``AgentSession._append_custom_entry``. This REPLACES the former RAM-only
        registry ``_entry_store``, which was lost on restart (G4): the entry now
        persists, survives a reload, and is readable back through ``ctx.entries()``
        (the reconstruction path S56's ``TreeStore`` builds on).

        It is deliberately NOT a message: ``ConversationTree`` never folds a
        ``customEntry`` into the loop context and ``convert_to_llm`` never sees it,
        so this is tree-as-backplane state — on the durable path, excluded from
        model input. To inject a node the model reads, use ``send_message``
        (``visible_to_model``) or ``send_user_message`` instead.

        Raises:
            RuntimeError: if no session with ``_append_custom_entry`` is bound (e.g.
                a bare ``ExtensionAPI()``). Fail-Early: raise rather than silently
                drop the entry into a RAM store that evaporates on restart.
            ValueError: propagated from ``_append_custom_entry`` when ``custom_type``
                is empty or ``data`` is not a dict.
        """
        if not hasattr(self._session, "_append_custom_entry"):
            raise RuntimeError(
                "append_entry: no session with a custom-entry log is bound "
                "(the entry would have nowhere durable to land)"
            )
        self._session._append_custom_entry(custom_type, data)

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
            RuntimeError: if no session with a message queue is bound (e.g. a bare
                ``ExtensionAPI()`` with no session). Fail-Early: raise rather than
                silently drop the message (the old ``hasattr`` no-op).
        """
        if deliver_as not in ("followUp", "nextTurn"):
            raise ValueError(
                "send_user_message: deliver_as must be 'followUp' or 'nextTurn', "
                f"got {deliver_as!r}"
            )
        if not hasattr(self._session, "_queue_message"):
            raise RuntimeError("send_user_message: no session with a message queue is bound")
        self._session._queue_message(content, deliver_as=deliver_as)

    def send_message(self, message: dict, options: dict | None = None) -> None:
        """Append a durable custom message node onto the active path (pi ``sendMessage``).

        Persists ``{customType, content, display?, details?}`` as a ``role:
        "custom"`` tree node via ``AgentSession._append_custom_message`` (E6 §2 /
        S38) — it renders in the transcript / tree and survives a reload, exactly
        like a ``before_agent_start`` injection.

        Per D-E6-1 the node is **display-only by default**. Pass
        ``options={"visible_to_model": True}`` to also feed it to the model
        (remapped custom→user on the wire); otherwise it is excluded from
        ``convert_to_llm`` and never reaches the LLM. This is intentional: it does
        NOT create a third model-visible default channel — ``before_agent_start``
        and ``send_user_message`` already serve that.

        Raises:
            RuntimeError: if no session with ``_append_custom_message`` is bound
                (e.g. a bare ``ExtensionAPI()``). Fail-Early: raise rather than
                silently drop the message (the old inert no-op called a nonexistent
                method and did nothing).
            ValueError: propagated from ``_append_custom_message`` when ``message``
                lacks ``content`` or ``customType``.
        """
        if not hasattr(self._session, "_append_custom_message"):
            raise RuntimeError(
                "send_message: no session with a custom-message log is bound "
                "(the message would have nowhere durable to land)"
            )
        self._session._append_custom_message(message, options or {})

    @property
    def ui(self) -> ExtensionUI:
        """UI methods (TUI-only, no-ops in headless mode).

        Returns:
            The ExtensionUI instance from the context.
        """
        return self._context._ui

    @property
    def config(self) -> dict[str, Any]:
        """This extension's per-run config slice (E6 §2 / S40).

        The dict sourced from ``~/.tau/config.json`` under
        ``"extensions": {"<name>": {…}}`` (keyed by this extension's file stem),
        with per-run ``--ext-config <name>.<key>=<value>`` overrides applied on
        top (CLI > config.json). An unconfigured extension reads ``{}`` — the
        extension supplies its own defaults (Fail-Early: the harness never
        fabricates a value it wasn't given). Values from config.json keep their
        JSON types; a ``--ext-config`` override is JSON-decoded when it parses
        (so ``ceiling=5.0`` → ``float``, ``paths=["a","b"]`` → ``list``) and kept
        as a plain string otherwise.
        """
        return self._config

    @property
    def context(self) -> ExtensionContext:
        """The ExtensionContext for this API."""
        return self._context

"""τ-agent-core extensions runner — the return-collecting hook dispatcher.

Reference: docs/EXTENSIONS-IMPLEMENTATION.md E1.3 / §8 step S5.
pi source of truth: ``coding-agent/src/core/extensions/runner.ts`` (``ExtensionRunner``).

This is a SEPARATE dispatcher that lives *alongside* the notify ``EventBus``
(``tau_agent_core.events``). The bus stays fire-and-forget for the 10
``AgentEvent`` types; this runner owns the **mutating** hook events, whose whole
point is that handler return values are collected and threaded forward.

pi keeps the two apart (``types.ts:1347``) and so does τ (§7 decision E1-a): the
hooks are a **parallel typed dispatch**, *not* an extension of the ``AgentEvent``
Literal. E1/S5 lands this dispatcher; E2 lands the hook **call-sites** in the
loop / session that actually invoke ``emit_tool_call`` / ``emit_tool_result`` /
``emit_before_agent_start``. (The ``context`` hook + its ``emit_context`` were
removed in E5 §3.2 / S30 — see the ``HOOK_EVENTS`` note below.)

Ordering contract (pi parity): extensions are iterated in **load order** (the
order of ``ExtensionHandlers`` in the runner) and, within an extension, handlers
run in **registration order** (append order of ``handlers[event]``). This matches
pi's nested ``for ext … for handler …`` walk (``runner.ts:740-768``).
"""

from __future__ import annotations

import inspect
import sys
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable

if TYPE_CHECKING:
    from tau_agent_core.extension_types import ExtensionContext

# A hook handler is called as ``handler(event, ctx)`` and may be sync or async.
# ``event`` is a plain mutable dict (pi's "mutate event.input in place"); the
# return value — when present — is the collected/threaded result.
HookHandler = Callable[..., Any]


@dataclass
class ExtensionError:
    """A non-fatal error raised by an extension hook handler.

    Mirrors pi's ``ExtensionError`` (``types.ts``): the offending extension's
    path, the event that was being dispatched, and the error message. Surfaced
    via :meth:`ExtensionRunner.on_error` so nothing is dropped silently.
    """

    extension_path: str
    event: str
    error: str


@dataclass
class ExtensionHandlers:
    """What a single extension registered — its hook handlers, tools, and commands.

    Mirrors the ``handlers: Map<string, HandlerFn[]>`` slice of pi's ``Extension``
    (``types.ts:1581``). One instance per loaded extension; the runner holds them
    in load order. Handlers for a given event are stored in registration order.

    ``tools`` / ``commands`` are the names this extension contributed via
    ``api.register_tool`` / ``api.register_command``. The registry itself stores
    tools/commands globally (by name), with no per-extension attribution, so this
    bucket is the one place that records *which* extension registered *what* —
    exactly what the ``/extensions`` surface reads (E5 §5 / S34). Recorded in
    registration order.
    """

    path: str
    handlers: dict[str, list[HookHandler]] = field(default_factory=dict)
    tools: list[str] = field(default_factory=list)
    commands: list[str] = field(default_factory=list)

    def on(self, event: str, handler: HookHandler) -> None:
        """Register ``handler`` for ``event`` (appended in registration order)."""
        self.handlers.setdefault(event, []).append(handler)


ErrorListener = Callable[[ExtensionError], None]


class ExtensionRunner:
    """Return-collecting dispatcher for the three mutating hook events.

    The dispatched events (§8, E2 wires the call-sites):

    - ``tool_call``          — veto / in-place arg patch (fail-CLOSED: a throwing
                               handler propagates so the call-site can block).
    - ``tool_result``        — field-patch the shared result event (later handlers
                               see earlier patches).
    - ``before_agent_start`` — ``system_prompt`` chains (last wins, live to later
                               handlers); ``message`` values accumulate.

    (A fourth hook, ``context`` — per-call replace of the message list — existed
    through E2 but was ELIMINATED in E5 §3.2 / S30. Under the durable-hook
    invariant the model's input is exactly the system prompt + the linear active
    path, so a per-send transform is a hidden divergence; its cases fold into
    durable ``tool_result`` edits + ``before_agent_start``. ``context`` is
    therefore no longer a hook event and ``api.on("context", …)`` raises.)

    Alongside the mutating hooks the runner also owns the two **notify-grade
    session-lifecycle hooks** (E6 §2 / S41): ``session_start`` and
    ``session_shutdown``. These collect **no** return value — they exist for
    setup/teardown side effects (watchers, state reconstruction from
    ``ctx.entries()``, exit commits) — but their handler exceptions are still
    SURFACED via :meth:`on_error` (the S44 regime), *not* swallowed like the
    fire-and-forget notify ``EventBus`` path. Routing them through the runner (not
    the bus) is exactly what buys the error surfacing. The frontends drive the
    lifecycle moments: ``session_start`` after extensions load; ``session_shutdown``
    on TUI quit, headless completion, and SIGINT/SIGTERM.

    ``has_handlers(event)`` gives call-sites the zero-extension fast path
    (pi ``agent-session.ts:405-411``): when it returns ``False`` the caller skips
    the dispatch entirely and the emit methods themselves also short-circuit to the
    identity result without doing any work.
    """

    #: The mutating hook events this dispatcher owns (E2 supplies the call-sites).
    HOOK_EVENTS = ("tool_call", "tool_result", "before_agent_start")

    #: The notify-grade session-lifecycle hooks (S41): no return effect, but
    #: error-surfaced through :meth:`on_error` rather than swallowed. Routed to a
    #: runner bucket (like ``HOOK_EVENTS``) so ``api.on(...)`` reaches the same
    #: error-surfacing dispatcher — see :meth:`ExtensionAPI.on`.
    LIFECYCLE_EVENTS = ("session_start", "session_shutdown")

    def __init__(
        self,
        extensions: list[ExtensionHandlers] | None = None,
        context: ExtensionContext | None = None,
    ) -> None:
        """Create a runner over ``extensions`` (load order preserved).

        ``context`` is the :class:`ExtensionContext` passed as the second argument
        to every handler (pi parity: the runner always hands handlers a ctx). When
        omitted an empty ``ExtensionContext`` is used; the session binds the live
        one via :meth:`set_context` when it wires the dispatcher (E2). This is a
        real, empty context object — not fabricated data — so the emit methods
        never invent a handler argument.
        """
        self._extensions: list[ExtensionHandlers] = list(extensions or [])
        if context is None:
            from tau_agent_core.extension_types import ExtensionContext as _Ctx

            context = _Ctx()
        self._context: ExtensionContext = context
        self._error_listeners: list[ErrorListener] = []

    # ------------------------------------------------------------------
    # Registration / wiring
    # ------------------------------------------------------------------

    def register_extension(self, path: str) -> ExtensionHandlers:
        """Append a new extension handler-group and return it for registration.

        The returned :class:`ExtensionHandlers` is appended in load order; call its
        :meth:`ExtensionHandlers.on` to register hook handlers in registration
        order.
        """
        group = ExtensionHandlers(path=path)
        self._extensions.append(group)
        return group

    def set_context(self, context: ExtensionContext) -> None:
        """Bind the live :class:`ExtensionContext` handed to hook handlers."""
        self._context = context

    def on_error(self, listener: ErrorListener) -> Callable[[], None]:
        """Register a listener for hook-handler errors. Returns an unsubscribe."""
        self._error_listeners.append(listener)

        def unsubscribe() -> None:
            try:
                self._error_listeners.remove(listener)
            except ValueError:
                pass

        return unsubscribe

    def has_handlers(self, event: str) -> bool:
        """Whether any loaded extension has a handler for ``event`` (fast path)."""
        for ext in self._extensions:
            if ext.handlers.get(event):
                return True
        return False

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _emit_error(self, error: ExtensionError) -> None:
        """Surface a hook-handler error; never drop it silently (Fail-Early).

        Notifies every registered listener. With no listener bound the error is
        written to stderr rather than swallowed — pi routes it to a listener the
        app installs; τ additionally refuses the silent-drop path.
        """
        if self._error_listeners:
            for listener in self._error_listeners:
                listener(error)
        else:
            print(
                f"[τ] extension error in {error.extension_path} ({error.event}): {error.error}",
                file=sys.stderr,
            )

    async def _call(self, handler: HookHandler, event: dict[str, Any]) -> Any:
        """Invoke ``handler(event, ctx)``, awaiting an async handler."""
        result = handler(event, self._context)
        if inspect.isawaitable(result):
            result = await result
        return result

    # ------------------------------------------------------------------
    # Hook dispatch — pi runner.ts parity
    # ------------------------------------------------------------------

    async def emit_tool_call(self, event: dict[str, Any]) -> dict[str, Any] | None:
        """Dispatch ``tool_call``; first ``block: true`` short-circuits.

        pi ``runner.ts:862-883``. Handlers may mutate ``event["input"]`` in place
        (later handlers see the mutation — no re-validation). The threaded result
        is the last truthy handler result; a ``block`` result returns immediately.

        Fail-CLOSED: handler exceptions are **not** caught here — they propagate so
        the E2 call-site can convert a throw into a block (pi does the same:
        ``emitToolCall`` has no try/except).
        """
        result: dict[str, Any] | None = None
        for ext in self._extensions:
            handlers = ext.handlers.get("tool_call")
            if not handlers:
                continue
            for handler in handlers:
                handler_result = await self._call(handler, event)
                if handler_result:
                    result = handler_result
                    if result.get("block"):
                        return result
        return result

    async def emit_tool_result(self, event: dict[str, Any]) -> dict[str, Any] | None:
        """Dispatch ``tool_result``; field-patch the shared event, later sees earlier.

        pi ``runner.ts:812-860``. Clone the event once, then let each handler patch
        ``content`` / ``details`` / ``is_error`` (whole-value replace, no deep
        merge). Returns the patched fields when anything changed, else ``None`` so
        the call-site passes the original result through unchanged.
        """
        current = dict(event)
        modified = False
        for ext in self._extensions:
            handlers = ext.handlers.get("tool_result")
            if not handlers:
                continue
            for handler in handlers:
                try:
                    handler_result = await self._call(handler, current)
                except Exception as err:  # noqa: BLE001 — surfaced, not dropped
                    self._emit_error(ExtensionError(ext.path, "tool_result", str(err)))
                    continue
                if not handler_result:
                    continue
                # pi checks `!== undefined`: a present key patches (even to None).
                if "content" in handler_result:
                    current["content"] = handler_result["content"]
                    modified = True
                if "details" in handler_result:
                    current["details"] = handler_result["details"]
                    modified = True
                if "is_error" in handler_result:
                    current["is_error"] = handler_result["is_error"]
                    modified = True
        if not modified:
            return None
        return {
            "content": current.get("content"),
            "details": current.get("details"),
            "is_error": current.get("is_error"),
        }

    async def emit_before_agent_start(
        self,
        prompt: str,
        images: list[dict[str, Any]] | None,
        system_prompt: str,
    ) -> dict[str, Any] | None:
        """Dispatch ``before_agent_start``; chain ``system_prompt``, accumulate messages.

        pi ``runner.ts:980-1044``. ``system_prompt`` chains — the latest value is
        threaded into each subsequent handler's event (last wins). Each handler's
        ``message`` accumulates. Returns ``{messages, system_prompt}`` (either key
        ``None`` when untouched) only if something changed, else ``None``.
        """
        current_system_prompt = system_prompt
        messages: list[Any] = []
        system_prompt_modified = False
        for ext in self._extensions:
            handlers = ext.handlers.get("before_agent_start")
            if not handlers:
                continue
            for handler in handlers:
                event = {
                    "type": "before_agent_start",
                    "prompt": prompt,
                    "images": images,
                    "system_prompt": current_system_prompt,
                }
                try:
                    result = await self._call(handler, event)
                except Exception as err:  # noqa: BLE001 — surfaced, not dropped
                    self._emit_error(ExtensionError(ext.path, "before_agent_start", str(err)))
                    continue
                if not result:
                    continue
                if result.get("message") is not None:
                    messages.append(result["message"])
                if result.get("system_prompt") is not None:
                    current_system_prompt = result["system_prompt"]
                    system_prompt_modified = True
        if messages or system_prompt_modified:
            return {
                "messages": messages or None,
                "system_prompt": current_system_prompt if system_prompt_modified else None,
            }
        return None

    # ------------------------------------------------------------------
    # Session-lifecycle dispatch — notify-grade, error-surfaced (S41)
    # ------------------------------------------------------------------

    async def _emit_lifecycle(self, event_name: str, event: dict[str, Any]) -> None:
        """Dispatch a notify-grade session-lifecycle hook (S41).

        Iterates extensions in load order and handlers in registration order (the
        same walk as the mutating hooks), calling each ``handler(event, ctx)`` and
        awaiting an async handler. Unlike the mutating hooks, the return value is
        **discarded** — these hooks have no path effect; they run for their side
        effects (watchers, ``ctx.entries()`` reconstruction, exit commits).

        Handler exceptions are SURFACED via :meth:`_emit_error` (the S44 regime)
        and dispatch continues to the next handler — one extension's failing
        teardown must neither abort another's nor be swallowed (Fail-Early: no
        silent drop, but also no fail-closed here, since a lifecycle hook has no
        result to gate on).
        """
        for ext in self._extensions:
            handlers = ext.handlers.get(event_name)
            if not handlers:
                continue
            for handler in handlers:
                try:
                    await self._call(handler, event)
                except Exception as err:  # noqa: BLE001 — surfaced, not dropped
                    self._emit_error(ExtensionError(ext.path, event_name, str(err)))

    async def emit_session_start(self, event: dict[str, Any]) -> None:
        """Dispatch ``session_start`` (notify-grade; return discarded, errors surfaced)."""
        await self._emit_lifecycle("session_start", event)

    async def emit_session_shutdown(self, event: dict[str, Any]) -> None:
        """Dispatch ``session_shutdown`` (notify-grade; return discarded, errors surfaced)."""
        await self._emit_lifecycle("session_shutdown", event)

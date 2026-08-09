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

    ``tools`` / ``commands`` / ``shortcuts`` are the names/keys this extension
    contributed via ``api.register_tool`` / ``api.register_command`` /
    ``api.register_shortcut``. The registry itself stores them globally (by
    name/key), with no per-extension attribution, so this bucket is the one place
    that records *which* extension registered *what* — exactly what the
    ``/extensions`` surface reads (E5 §5 / S34; shortcuts E10 §6 / S69). Recorded in
    registration order.
    """

    path: str
    handlers: dict[str, list[HookHandler]] = field(default_factory=dict)
    tools: list[str] = field(default_factory=list)
    commands: list[str] = field(default_factory=list)
    shortcuts: list[str] = field(default_factory=list)

    def on(self, event: str, handler: HookHandler) -> None:
        """Register ``handler`` for ``event`` (appended in registration order)."""
        self.handlers.setdefault(event, []).append(handler)


ErrorListener = Callable[[ExtensionError], None]

#: Declared discourse position for a ``before_agent_start`` injected message
#: (§12.4's tempo table, corrected by §16.5). ``"before_user"`` threads the message
#: AHEAD of the user's utterance — the *phasic* position, results attached as
#: context deliberation begins from. ``"after_user"`` threads it behind the
#: utterance, which is pi's order and therefore the default for a message that does
#: not declare one.
MESSAGE_POSITION_BEFORE_USER = "before_user"
MESSAGE_POSITION_AFTER_USER = "after_user"
MESSAGE_POSITIONS = (MESSAGE_POSITION_BEFORE_USER, MESSAGE_POSITION_AFTER_USER)

#: The firing unit each turn-boundary hook event carries, as a field ON the event.
#:
#: Two hooks in this harness fire at turn boundaries and they count different
#: turns: ``turn_end`` once per assistant completion, ``user_turn_end`` once per
#: ``AgentSession.prompt()``. A handler receives a bare event dict, so without this
#: field it holds a count whose unit it cannot state — it has to know the cadence
#: from documentation it may have read about a different hook. That is §9 rule 1
#: one layer down, and it is the same defect ``Trace.arm`` was added to close in
#: freeze v1.1: a partition key that lived only in the surrounding directory, so
#: any consumer holding the bare record had lost it and could pool two populations
#: with nothing detecting the mix.
FIRING_UNIT_AGENT_LOOP_TURN = "agent_loop_turn"
FIRING_UNIT_USER_TURN = "user_turn"


class ExtensionRunner:
    """Return-collecting dispatcher for the mutating hook events.

    The dispatched events (§8, E2 wires the call-sites):

    - ``tool_call``          — veto / in-place arg patch (fail-CLOSED: a throwing
                               handler propagates so the call-site can block).
    - ``tool_result``        — field-patch the shared result event (later handlers
                               see earlier patches).
    - ``before_agent_start`` — ``system_prompt`` chains (last wins, live to later
                               handlers); ``message`` values accumulate. A returned
                               message may DECLARE its discourse position
                               (``"position": "before_user" | "after_user"``); see
                               :meth:`emit_before_agent_start`.
    - ``input``              — transform the user prompt BEFORE the user node
                               exists (``prompt``/``images`` chain, later handlers
                               see the running value); ``handled: True`` consumes
                               the input without starting a turn (S42).
    - ``turn_end``           — MUTATING variant (S43, τ divergence D-E6-6): a
                               handler may return ``{message}`` → the loop appends
                               it as a durable ``customMessage`` node BEFORE the
                               next turn (append-only, same power/limits as
                               ``before_agent_start``); returning nothing is a PURE
                               OBSERVER, preserving pi's notify-grade ``turn_end``.
                               Fires once per AGENT-LOOP turn (per assistant
                               completion), NOT once per user turn — see
                               :meth:`emit_turn_end`.
    - ``user_turn_end``      — fires exactly ONCE per ``AgentSession.prompt()``,
                               at the boundary ``turn_end`` is not: after the loop,
                               the followUp drain and auto-compaction. Same durable
                               ``{message}`` append as ``turn_end``. See
                               :meth:`emit_user_turn_end`.
    - ``session_before_switch`` — veto point for
                               :class:`~tau_agent_core.agent_session_runtime
                               .AgentSessionRuntime`'s ``new_session``/``fork``/
                               ``switch_session`` (H2, docs/REMOTE-CONTROL.md
                               §4[6]). A handler returning ``{"cancel": True}``
                               short-circuits the dispatch and the runtime
                               operation reports ``{"cancelled": True}`` without
                               touching anything. Mirrors pi's
                               ``emitBeforeSwitch``/``emitBeforeFork``
                               (``agent-session-runtime.ts:133-165``), collapsed
                               to ONE hook event because τ's simpler,
                               reset-in-place runtime does not distinguish "new
                               session" from "fork" the way pi's per-operation
                               ``SessionManager`` recreation does — both are just
                               a ``reason`` on the same event. See
                               :meth:`emit_session_before_switch`.

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

    #: The mutating hook events this dispatcher owns (E2 supplies the call-sites;
    #: S42 adds ``input``, fired pre-node at the top of ``AgentSession.prompt``;
    #: S43 adds ``turn_end``, fired per turn in the loop with a durable append;
    #: ``user_turn_end`` fires once per ``prompt()`` at the session tail).
    HOOK_EVENTS = (
        "tool_call",
        "tool_result",
        "before_agent_start",
        "input",
        "turn_end",
        "user_turn_end",
        "session_before_switch",
    )

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

    def get_extension(self, path: str) -> ExtensionHandlers | None:
        """The active bucket registered under ``path`` (``None`` if none/removed).

        Used by the runtime-management path (E10 §6 / S70) to find an extension's
        bucket for a targeted lifecycle emit + removal. Only ACTIVE buckets are held
        in ``_extensions``; a disabled extension's bucket has been removed, so this
        returns ``None`` for it — exactly the "hooks stop firing" signal.
        """
        for ext in self._extensions:
            if ext.path == path:
                return ext
        return None

    def remove_extension(self, path: str) -> ExtensionHandlers | None:
        """Detach and return the bucket for ``path`` so its hooks stop firing (S70).

        Removes the bucket from the load-order list every ``emit_*`` walks, so after
        this call the extension's handlers are no longer dispatched. Returns the
        removed bucket (or ``None`` if not present) — the caller has already fired the
        extension's ``session_shutdown`` teardown against it. Registry-level
        tools/commands/shortcuts are unwound separately by the session (this class
        owns only the hook buckets).
        """
        for i, ext in enumerate(self._extensions):
            if ext.path == path:
                return self._extensions.pop(i)
        return None

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
                        # Attribute the veto to THIS extension (S50, anchor G11).
                        # The runner is the one place that knows WHICH bucket
                        # blocked; the call-site threads this onto the blocked
                        # render + the JSON veto record. Copy so the handler's own
                        # dict is never mutated; ``setdefault`` lets a handler that
                        # deliberately names a different origin keep it.
                        blocked = dict(result)
                        blocked.setdefault("extension", ext.path)
                        return blocked
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

        **Discourse position (§12.4 / §16.5).** A returned ``message`` may carry a
        ``"position"`` key declaring where it is threaded relative to the user's
        utterance:

        - ``"after_user"`` (the default when the key is absent) — the message
          follows the user turn: ``[user, *nextTurn, *after_user]``. This is pi's
          order verbatim (``agent-session.ts:1089-1120``; the newer harness agrees
          for hook messages, ``agent-harness.ts:577``), so an extension that says
          nothing keeps pi-parity behaviour byte for byte.
        - ``"before_user"`` — the message PRECEDES the user turn:
          ``[*before_user, user, *nextTurn, *after_user]``. This is the *phasic*
          discourse position §12.4's tempo table means by "results attached before
          deliberation": the injected material reads to the model as context the
          utterance arrives into, not as material following it.

        The point of the key is that the position stops being an accident of the
        threading and becomes a property the reflex surface DECLARES. §16.5 records
        why that matters: a surface rendering written for one order and threaded in
        the other still runs, still passes, and reads differently to the model —
        a corrupted measurement rather than a crash.

        Raises:
            ValueError: on any ``position`` value other than those two. Fail-Early:
                an unrecognised position is not silently resolved to a plausible
                default, because "silently plausible" is exactly the failure this
                key exists to remove.
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
                    message = result["message"]
                    position = message.get("position", MESSAGE_POSITION_AFTER_USER)
                    if position not in MESSAGE_POSITIONS:
                        raise ValueError(
                            f"before_agent_start message from {ext.path!r} declares "
                            f"position={position!r}; the discourse position must be one of "
                            f"{list(MESSAGE_POSITIONS)}. Fail-Early — an unrecognised "
                            "position is not defaulted, because a reflex surface threaded "
                            "into the wrong discourse position still runs and still passes "
                            "(§12.4 / §16.5)."
                        )
                    messages.append(message)
                if result.get("system_prompt") is not None:
                    current_system_prompt = result["system_prompt"]
                    system_prompt_modified = True
        if messages or system_prompt_modified:
            return {
                "messages": messages or None,
                "system_prompt": current_system_prompt if system_prompt_modified else None,
            }
        return None

    async def emit_input(
        self,
        prompt: str,
        images: list[dict[str, Any]] | None,
        *,
        source: str = "interactive",
        submitter: str = "human",
    ) -> dict[str, Any]:
        """Dispatch ``input``; transforms chain, ``handled`` short-circuits.

        S42 / roadmap §2 (anchor G2). pi ``runner.ts:1095-1133`` (``emitInput``),
        flattened to τ's ``{prompt, images} -> {prompt?, handled?}`` contract.

        Fires BEFORE the user node exists (the call-site is now
        :meth:`~tau_agent_core.agent_session.AgentSession.submit`, moved there
        from ``prompt()`` per docs/SUBMISSION-LIFECYCLE.md "The one door" step 2
        so every submission source shares this pipeline), so a transformed prompt
        is the SINGLE copy that gets persisted, rendered, and sent — no invariant
        violation, exactly the reasoning that made ``before_agent_start`` legal.

        ``source``/``submitter`` are the originating
        :class:`~tau_agent_core.submission.Submission`'s provenance (pi's
        ``InputSource`` equivalent, absent from τ before the submission lifecycle
        work) — carried onto the event so a handler can branch on *who* submitted,
        e.g. treat a ``source="bus"`` input differently from one typed by a human.
        Defaulting to ``"interactive"``/``"human"`` keeps a direct call (as the
        existing test suite makes) equivalent to what every ``prompt()`` call
        implied before this parameter existed.

        Each handler receives ``{"type": "input", "prompt": <running text>,
        "images": <running images>, "source": <source>, "submitter": <submitter>}``
        and may return:

        - ``{"handled": True}`` — consume the input WITHOUT starting a turn (a
          command-like extension that showed its own feedback); dispatch stops
          immediately and the call-site returns without a turn.
        - ``{"prompt": <text>}`` and/or ``{"images": [...]}`` — replace the
          running prompt/images; later handlers see the replacement (chaining,
          like ``before_agent_start``'s ``system_prompt``).
        - ``None`` / ``{}`` — pass through unchanged.

        Returns ``{"handled": bool, "prompt": str, "images": ...}`` — the running
        values (on ``handled`` the call-site discards them with the turn).

        Handler exceptions are SURFACED via :meth:`on_error` (the S44 regime) and
        dispatch continues to the next handler — pi's ``emitInput`` wraps each
        handler in try/catch and continues likewise.
        """
        current_prompt = prompt
        current_images = images
        for ext in self._extensions:
            handlers = ext.handlers.get("input")
            if not handlers:
                continue
            for handler in handlers:
                event = {
                    "type": "input",
                    "prompt": current_prompt,
                    "images": current_images,
                    "source": source,
                    "submitter": submitter,
                }
                try:
                    result = await self._call(handler, event)
                except Exception as err:  # noqa: BLE001 — surfaced, not dropped
                    self._emit_error(ExtensionError(ext.path, "input", str(err)))
                    continue
                if not result:
                    continue
                if result.get("handled"):
                    return {
                        "handled": True,
                        "prompt": current_prompt,
                        "images": current_images,
                    }
                if result.get("prompt") is not None:
                    current_prompt = result["prompt"]
                if result.get("images") is not None:
                    current_images = result["images"]
        return {"handled": False, "prompt": current_prompt, "images": current_images}

    async def emit_turn_end(
        self,
        turn_index: int,
        usage: dict[str, Any] | None,
        messages: list[Any],
    ) -> list[Any]:
        """Dispatch the MUTATING ``turn_end`` hook; accumulate returned messages (S43).

        τ divergence from pi (D-E6-6): pi's ``turn_end`` is notify-only — its
        handler return is discarded (``agent-session.ts:617``). τ additionally lets
        a handler return ``{message}``, which the loop call-site appends as a durable
        ``customMessage`` node BEFORE the next turn, with the same append-only power
        and limits as ``before_agent_start`` (§1.3): it may APPEND to the active
        path, never rewrite a prior node. A handler that returns nothing (or no
        ``message`` key) is a PURE OBSERVER — the notify-grade ``turn_end`` behaviour
        pi ships, which τ preserves alongside the mutating variant (roadmap S43). The
        two coexist: an observing handler and a mutating handler both run, in load /
        registration order, on the same event.

        **Firing unit: the agent-loop turn, not the user turn (§12.4 / §16.5).** The
        call-sites are the four per-completion points in :meth:`AgentLoop.run` /
        :meth:`AgentLoop.run_continue`, so an utterance resolved in six tool
        round-trips fires this hook six times. That is correct and is pi's cadence
        (``agent-loop.ts:197``/``:218``, once per assistant message) — a
        per-completion observer needs a per-completion hook. It is NOT the
        *consolidative* tempo: mapping a once-per-utterance consolidation onto this
        hook runs it once per round-trip, which is not a crash and not a test
        failure, only a number wrong by a factor nobody measured. Use
        ``user_turn_end`` for that cadence (:meth:`emit_user_turn_end`).

        Each handler receives ``{"type": "turn_end", "firing_unit", "turn_index",
        "usage", "messages"}``. ``firing_unit`` is
        :data:`FIRING_UNIT_AGENT_LOOP_TURN`, carried ON the event so a handler
        holding it can state the unit of its own count instead of inferring the
        cadence from documentation. The rest: the just-finished turn's index, its
        per-completion token
        usage (or ``None``), and the messages it produced (assistant + tool
        results). Every returned ``message`` accumulates in order; the collected list
        is returned to the call-site (empty when nothing was returned, so the caller
        skips the append entirely).

        Handler exceptions are SURFACED via :meth:`on_error` (the S44 regime) and
        dispatch continues to the next handler — an observing handler must neither
        abort a mutating one nor be swallowed (Fail-Early: no silent drop).
        """
        injected: list[Any] = []
        for ext in self._extensions:
            handlers = ext.handlers.get("turn_end")
            if not handlers:
                continue
            for handler in handlers:
                event = {
                    "type": "turn_end",
                    "firing_unit": FIRING_UNIT_AGENT_LOOP_TURN,
                    "turn_index": turn_index,
                    "usage": usage,
                    "messages": messages,
                }
                try:
                    result = await self._call(handler, event)
                except Exception as err:  # noqa: BLE001 — surfaced, not dropped
                    self._emit_error(ExtensionError(ext.path, "turn_end", str(err)))
                    continue
                if not result:
                    continue
                if result.get("message") is not None:
                    injected.append(result["message"])
        return injected

    async def emit_user_turn_end(
        self,
        loop_turns: int,
        messages: list[Any],
    ) -> list[Any]:
        """Dispatch ``user_turn_end`` — the once-per-``prompt()`` boundary hook.

        **Why this exists.** §12.4's tempo table maps the *consolidative* tempo onto
        ``turn_end``, and §16.5 correction 2 records why that is wrong done
        literally: ``turn_end`` fires per agent-loop turn, so an utterance resolved
        in six tool round-trips runs six consolidation passes and six sets of
        durable writes. §16.5 names the two legitimate answers — "guard the handler
        on a turn boundary (the ``prompt()`` return is the boundary)" or "run
        consolidation from the node after ``prompt()`` returns". This hook is the
        first one, provided by the harness instead of re-derived (and re-got-wrong)
        by every extension that wants the flywheel cadence.

        **Firing unit, precisely.** Exactly once per :meth:`AgentSession.prompt`
        call that starts a turn, at its tail — AFTER the agent loop, AFTER the
        followUp re-entries (which run inside the same ``prompt()``), and AFTER
        auto-compaction and the deferred compact/fork drain. So the handler sees the
        whole user turn, in its final persisted form, once. It does NOT fire when
        an ``input`` handler consumed the input with ``handled: True`` (no turn
        started, so no turn ended), it does NOT fire when ``prompt()`` raises (there
        is no clean boundary to consolidate at — Fail-Early over a half-turn
        consolidation), and it does NOT fire for
        :meth:`AgentSession.continue_conversation`, which produces loop turns
        without a user turn.

        **This hook has no pi counterpart.** pi's ``turn_end`` is notify-only
        (``agent-session.ts:617``) and pi has no once-per-prompt mutating hook; its
        nearest relative is the harness's notify-grade ``settled``
        (``agent-harness.ts:533``). A deliberate τ divergence, recorded here and in
        ``AgentSession._run_user_turn_end``.

        Each handler receives ``{"type": "user_turn_end", "firing_unit",
        "loop_turns", "messages"}``. ``firing_unit`` is
        :data:`FIRING_UNIT_USER_TURN`, the counterpart of ``turn_end``'s
        :data:`FIRING_UNIT_AGENT_LOOP_TURN`; ``loop_turns``
        is the number of agent-loop turns this user turn consumed (assistant
        completions, followUp re-entries included) and the user turn's messages in
        persisted order. Returning ``{message}`` appends a durable ``customMessage``
        node (same power and limits as ``before_agent_start``: append-only, never a
        rewrite); returning nothing is a pure observer.

        Handler exceptions are SURFACED via :meth:`on_error` (the S44 regime) and
        dispatch continues to the next handler.
        """
        injected: list[Any] = []
        for ext in self._extensions:
            handlers = ext.handlers.get("user_turn_end")
            if not handlers:
                continue
            for handler in handlers:
                event = {
                    "type": "user_turn_end",
                    "firing_unit": FIRING_UNIT_USER_TURN,
                    "loop_turns": loop_turns,
                    "messages": messages,
                }
                try:
                    result = await self._call(handler, event)
                except Exception as err:  # noqa: BLE001 — surfaced, not dropped
                    self._emit_error(ExtensionError(ext.path, "user_turn_end", str(err)))
                    continue
                if not result:
                    continue
                if result.get("message") is not None:
                    injected.append(result["message"])
        return injected

    async def emit_session_before_switch(self, reason: str, target: str | None = None) -> bool:
        """Dispatch ``session_before_switch``; the first ``{"cancel": True}`` wins.

        H2 (docs/REMOTE-CONTROL.md §4[6]): the veto point
        :class:`~tau_agent_core.agent_session_runtime.AgentSessionRuntime` calls
        before ``new_session``/``fork``/``switch_session`` touch anything. Load-
        order iteration, short-circuiting on the FIRST handler that vetoes —
        pi's ``emitBeforeSwitch``/``emitBeforeFork`` return as soon as
        ``result?.cancel === true`` (``agent-session-runtime.ts:142-147``), and
        this does the same rather than polling every handler once the answer is
        already known.

        Each handler receives ``{"type": "session_before_switch", "reason",
        "target"}``. ``reason`` is ``"new"``, ``"resume"`` (switch_session), or
        ``"fork"`` — pi's own vocabulary (``SessionShutdownEvent["reason"]``,
        minus ``"quit"``, which this hook never fires for). ``target`` is the
        session id being switched/forked to when the caller supplied one
        (``switch_session``'s ``session_id``), else ``None``.

        Returns ``True`` (vetoed) or ``False`` (proceed) — deliberately a bare
        bool rather than the raw handler dict: the runtime's only decision here
        is "stop or continue", and threading a dict through would invite a
        second consumer to read fields this hook does not promise.

        Handler exceptions are SURFACED via :meth:`on_error` (the S44 regime)
        and dispatch continues to the next handler — one extension's failing
        veto check must not silently block (or silently fail to block) another
        extension's.
        """
        for ext in self._extensions:
            handlers = ext.handlers.get("session_before_switch")
            if not handlers:
                continue
            for handler in handlers:
                event = {"type": "session_before_switch", "reason": reason, "target": target}
                try:
                    result = await self._call(handler, event)
                except Exception as err:  # noqa: BLE001 — surfaced, not dropped
                    self._emit_error(ExtensionError(ext.path, "session_before_switch", str(err)))
                    continue
                if result and result.get("cancel"):
                    return True
        return False

    def emit_veto_record(self, *, tool_name: str, reason: str, extension: str | None) -> None:
        """Emit a JSON-stream veto record for a blocked tool call (S50 — anchor G11).

        Delegates to the bound :class:`ExtensionContext`, which routes to the shared
        :class:`ExtensionUI`'s headless record sink — the parallel record family the
        ``--mode json`` frontend writes alongside the closed ``AgentEvent`` set (S49).
        The record carries ``blocked: true`` so an orchestrator reading a child
        ``tau -p --mode json`` stream can tell a veto from a generic tool error. With
        no sink installed (the TUI / ``--mode text``) this is a no-op — the veto still
        surfaces on the ``tool_execution_end`` AgentEvent's ``blocked`` field there.
        """
        self._context.emit_veto_record(extension=extension, tool=tool_name, reason=reason)

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
            await self._emit_lifecycle_one(ext, event_name, event)

    async def _emit_lifecycle_one(
        self, ext: ExtensionHandlers, event_name: str, event: dict[str, Any]
    ) -> None:
        """Dispatch a notify-grade lifecycle hook to a SINGLE extension bucket (S70).

        The per-bucket unit of :meth:`_emit_lifecycle`, split out so runtime
        management (enable/disable/reload) can fire ``session_start`` /
        ``session_shutdown`` for just the affected extension — e.g. a clean teardown
        on disable without touching its peers. Same error-surfacing (S44) and
        registration-order walk as the whole-runner variant.
        """
        handlers = ext.handlers.get(event_name)
        if not handlers:
            return
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

    async def emit_session_start_for(self, ext: ExtensionHandlers, event: dict[str, Any]) -> None:
        """Fire ``session_start`` for a single bucket (S70 enable/reload bring-up)."""
        await self._emit_lifecycle_one(ext, "session_start", event)

    async def emit_session_shutdown_for(
        self, ext: ExtensionHandlers, event: dict[str, Any]
    ) -> None:
        """Fire ``session_shutdown`` for a single bucket (S70 disable/reload teardown)."""
        await self._emit_lifecycle_one(ext, "session_shutdown", event)

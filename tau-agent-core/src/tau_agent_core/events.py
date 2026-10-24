"""τ-agent-core events: AgentEvent type for the central event bus.

Reference: SUBPHASE-0.0.md, "5. Agent Events (tau-agent-core)" section.

All agent events are emitted by AgentLoop.run() and consumed by:
- The TUI (tau-coding-agent)
- Extensions (via EventBus)
- Session persistence (via SessionManager)

Event types:
- agent_start, agent_end
- turn_start, turn_end
- message_start, message_update, message_end
- tool_execution_start, tool_execution_update, tool_execution_end
- provider_retry
- follow_ups_dropped
- suspension_open, suspension_closed (docs/STEP-DEBUGGER.md §3.4)
- lock_changed

Constraint: "fire-and-forget" here means FAILURE ISOLATION, never scheduling.
A handler that raises does not kill its siblings or its emitter; it is surfaced
and the remaining handlers still run. Emission itself is *awaited*: ``emit``
calls each handler in turn and, when the call returns a coroutine, awaits it
before moving on — so a slow handler slows the emitter. That is load-bearing,
not incidental: it is what lets a subscriber apply backpressure to the agent
loop (see ``tau_agent_core.rpc``'s T3 credit gate, and §4[1] T3 / §10 of
docs/REMOTE-CONTROL.md, which records getting this exact distinction wrong).
"""

from __future__ import annotations

import asyncio
import contextlib
import sys
from typing import Any, Callable, Literal

from pydantic import BaseModel, Field

from tau_agent_core.policies import HandlerPolicyError
from tau_agent_core.submission import SubmissionSource
from tau_agent_core.suspension import outside_dispatch
from tau_llm.docs import agent_facing
from tau_llm.errors import ProviderConnectionError, ProviderHttpError, ProviderStreamError
from tau_llm.retry import DEFAULT_RETRY_POLICY

ErrorListener = Callable[[BaseException, str], None]

AgentEndReason = Literal[
    "done",
    "terminate",
    "aborted",
    "max_turns",
    "repeat_tool_calls",
    "error",
    "suspended",
]

SideCompletionPurpose = Literal["compaction", "branch_summary"]
"""Which piece of side work a ``side_completion_*`` event is reporting.

Side work is a completion τ makes on its own behalf rather than on the model's:
it spends tokens, it produces text a reader wants to see, and it belongs to no
turn. The two are the compaction summary and a branch summary.
"""

SideCompletionReason = Literal["manual", "threshold", "navigate"]
"""What ASKED for a side completion — a different question from what it is.

``manual`` is a person or a host: ``/compact`` and the ``compact`` RPC verb.
``threshold`` is ``_maybe_auto_compact``, which nobody asked for and which is the
one a reader is most likely to be surprised by. ``navigate`` is the tree
browser's summarising move, the only thing that raises a branch summary.

pi's equivalent has a third compaction reason, ``overflow`` — compact and retry
after a request came back over the window. τ has no such recovery path
(``_perform_compaction`` has exactly two callers), so declaring the value would
put a branch on the wire that nothing can reach.
"""

SuspensionCloseReason = Literal["answered", "aborted", "cursor_closed", "abandoned"]
"""Why a ``suspension_closed`` closed (docs/STEP-DEBUGGER.md §3.4).

``answered``: a responder answered it. ``aborted``: its turn's abort signal
tripped, an ``abort`` answer to an earlier one included. ``cursor_closed``: the
session shut down under it. ``abandoned``: its task was cancelled.
"""


@agent_facing(topic="events")
class ErrorDetail(BaseModel):
    """A failure as fields, beside the ``error`` string on ``agent_end`` and ``provider_retry``.

    See docs/RESPONSE-HEADERS.md §5.

    Attributes:
        type: The exception's class name, e.g. ``"ProviderHttpError"``. Always set.
        status: The HTTP status of a ``ProviderHttpError``, or the HTTP-like
            code a ``ProviderStreamError``'s frame named; None otherwise.
        retry_after: Seconds the server asked the client to wait, parsed from
            ``Retry-After``; None when it sent none or this is not an HTTP refusal.
        headers: The refusal's response headers, names lowercased; None when
            none were captured, which includes every failure that is not an HTTP
            refusal.
        retryable: Whether ``tau_llm.retry.DEFAULT_RETRY_POLICY`` counts this
            failure as transient: True for a retryable status or a connection
            failure, False for any other HTTP refusal and for a
            ``ProviderStreamError``, None for an exception τ makes no
            transience judgement about. On ``agent_end`` a True means
            τ's own retries ran out, or the server asked for a longer wait than
            τ will make inside a turn.
        key_sent: A ``ProviderHttpError``'s ``key_sent``: whether the request
            carried a key, which tells "no API key" from "API key refused" on a
            401; None when not an HTTP refusal or the provider did not say.
    """

    type: str
    status: int | None = None
    retry_after: float | None = None
    headers: dict[str, str] | None = None
    retryable: bool | None = None
    key_sent: bool | None = None

    @classmethod
    def of(cls, exc: BaseException) -> "ErrorDetail":
        """The detail for ``exc``.

        Args:
            exc: The exception that ended the loop or was retried.

        Returns:
            Every field filled for a ``ProviderHttpError``; ``type`` and
            ``retryable`` for a ``ProviderConnectionError``, plus ``status`` for
            a ``ProviderStreamError`` whose frame named an HTTP-like code;
            ``type`` alone for anything else.
        """
        if isinstance(exc, ProviderHttpError):
            return cls(
                type=type(exc).__name__,
                status=exc.status_code,
                retry_after=exc.retry_after,
                headers=exc.headers,
                retryable=DEFAULT_RETRY_POLICY.retryable(exc),
                key_sent=exc.key_sent,
            )
        if isinstance(exc, (ProviderConnectionError, ProviderStreamError)):
            return cls(
                type=type(exc).__name__,
                status=exc.status if isinstance(exc, ProviderStreamError) else None,
                retryable=DEFAULT_RETRY_POLICY.retryable(exc),
            )
        return cls(type=type(exc).__name__)


@agent_facing(topic="events")
class AgentEvent(BaseModel):
    """A single event from the agent loop.

    Reference: SUBPHASE-0.0.md, "5. Agent Events" section.
    Reference: docs/SUBMISSION-LIFECYCLE.md, "Provenance on events" (phase 2).

    Attributes:
        type: Event type discriminator
        timestamp: Milliseconds since epoch
        message: Message data (agent_start/end, message_*)
        turn_index: Turn number (turn_*)
        tool_call_id: Tool call ID (tool_*)
        tool_name: Tool name (tool_*)
        args: Tool execution arguments (tool_execution_start)
        result: Tool execution result (tool_execution_*)
        details: A tool's structured facts about its own execution on
            ``tool_execution_end`` — the path it read, the line range, the match
            count, the diff. What ``result`` holds is what the MODEL reads; this
            is what a head can render beside it. ``None`` when the tool declared
            none, and on every other event type.
        is_error: Whether this event represents an error
        blocked: Whether a ``tool_execution_end`` is an extension VETO (S50) —
            distinct from a generic errored result, so a front-end can render
            "⛔ blocked by <ext>: <reason>" rather than a plain error box.
        blocked_by: The extension that vetoed the call (its runner path label),
            paired with ``blocked`` on a ``tool_execution_end``; ``None`` otherwise.
        parent_tool_call_id: On a ``tool_execution_*`` event for a call a tool
            made (``ctx.execute_tool``), the calling tool's call id. ``None`` for
            a call the model made. Experimental.
        tool_results: List of tool result messages (turn_end)
        messages: List of messages produced (agent_end)
        error: Why an ``agent_end`` closed, when the loop raised rather than
            finishing (``"RuntimeError: Connection refused"``). ``None`` on a
            normal close; always paired with ``is_error=True``. On a
            ``provider_retry`` it is the failure being retried, with
            ``is_error=False`` because the turn goes on.
        error_detail: ``error`` as fields (:class:`ErrorDetail`), set exactly
            when ``error`` is, so a handler branches on a status or a class name
            instead of parsing the string.
        response_headers: The HTTP response headers of the completion a
            ``message_end`` closes, names lowercased. Set only on the
            usage-bearing ``message_end``; None there when the transport did not
            capture them, and ``{}`` when the server sent none. Not persisted:
            the message in the session log never carries them.
        end_reason: How an ``agent_end`` closed, as one of ``"done"``,
            ``"terminate"``, ``"aborted"``, ``"max_turns"``,
            ``"repeat_tool_calls"``, ``"error"`` or ``"suspended"`` (the turn
            unwound at a suspension, docs/STEP-DEBUGGER.md §3.2.2). ``None`` on
            every other event type. This distinguishes a loop that finished from one that was cut
            short: before it, a run stopped by ``max_turns`` emitted the same
            ``agent_end`` as one where the model simply had nothing more to say,
            so a caller could not tell a truncated answer from a complete one.
        cursor_id: The :class:`~tau_agent_core.cursor.Cursor` whose turn emitted
            this event. Several cursors run turns on one session at once, so a
            renderer routes by it (docs/CURSORS.md §6). ``None`` outside a turn.
        submission_id: The ``Submission`` that drove this turn, if any — Jupyter's
            ``parent_header``. ``None`` for a turn not driven through ``submit()``
            (e.g. ``continue_conversation()``, which predates the Submission
            contract) — an honest "no submission", never a fabricated id.
        source: The submission's origin (``"interactive"``, ``"bus"``, ``"agent"``,
            …) — pi's ``InputSource`` equivalent, so a renderer can decide *how* to
            show a turn (Jupyter's rule: render every source, differently) without
            the core knowing any renderer exists. ``None`` alongside
            ``submission_id``.
        submitter: WHO submitted — an extension name, ``"human"``, a channel id.
            ``None`` alongside ``submission_id``.
        correlation: The submission's free-form origin detail (bus subject, cron
            id, HTTP request id), carried through unchanged so an embedded server
            can fan out to the right stream. ``None`` alongside ``submission_id``
            (an EMPTY dict would claim "a submission with no correlation data";
            ``None`` says "no submission stamped this event" instead).
        purpose: Which side completion a ``side_completion_*`` event reports —
            ``"compaction"`` or ``"branch_summary"``. ``None`` on every other
            type. Side work belongs to no turn and stamps no ``submission_id``,
            so this is what a renderer keys on instead.
        reason: What asked for it — ``"manual"``, ``"threshold"`` or
            ``"navigate"``. Set on all three ``side_completion_*`` events, so a
            reader that joins late still learns whether the compaction it is
            watching was requested or imposed. ``None`` on every other type.
        delta: One text fragment of a side completion, on
            ``side_completion_update``. ``None`` elsewhere.
        text: The finished side-completion text, on ``side_completion_end``.
            Sent whole as well as in fragments, because a subscriber that
            attached late or dropped a delta must still be able to render the
            result rather than a partial one. ``None`` elsewhere, and ``None``
            on an end that failed — paired with ``is_error`` and ``error``.
        usage: What a side completion SPENT, on ``side_completion_end``. This is
            the only place those tokens are observable: the work runs outside the
            agent loop, so no ``turn_end`` counts it (docs/STREAMING-SIDE-WORK.md).
        attempt: Which retry a ``provider_retry`` announces, from 1.
        max_retries: The most retries that request will make.
        delay_seconds: How long τ waits before re-sending it; the status being
            retried is ``error_detail.status`` (docs/PROVIDER-RETRY.md §5).
        dropped: The texts a user abort discarded, on ``follow_ups_dropped``
            only: queued followUps, then waiting ``enqueue`` submissions
            (docs/TURN-SETTINGS.md §5, §5.1). Written nowhere else.
        suspension: On ``suspension_open`` and ``suspension_closed``, the
            suspension's state (:meth:`~tau_agent_core.suspensions.Suspension.state`):
            its id, cursor and owner linkage, spec, site and whether it is
            queued. ``suspension_open`` is sent again, ``queued`` false, when a
            queued one reaches the front of its cursor's queue.
        resolution: On ``suspension_closed``, what ``api.suspend`` returned:
            ``{id, action, values, answered_by, at_ms}``.
        close_reason: On ``suspension_closed``, why it closed
            (:data:`SuspensionCloseReason`). ``None`` on every other type.
        read_only: On ``lock_changed``, whether this process may no longer
            write the head cursor's tree.
        lock_holder: On ``lock_changed``, the process holding the tree's lock
            (``LockHolder.to_dict``), or ``None``.
    """

    type: Literal[
        "agent_start",
        "agent_end",
        "turn_start",
        "turn_end",
        "message_start",
        "message_update",
        "message_end",
        "tool_execution_start",
        "tool_execution_update",
        "tool_execution_end",
        "side_completion_start",
        "side_completion_update",
        "side_completion_end",
        "provider_retry",
        "follow_ups_dropped",
        "suspension_open",
        "suspension_closed",
        "lock_changed",
    ]

    timestamp: int = Field(ge=0)

    # Conditional fields
    message: dict[str, Any] | None = None
    turn_index: int | None = None
    tool_call_id: str | None = None
    tool_name: str | None = None
    args: dict[str, Any] | None = None
    result: Any | None = None
    details: dict[str, Any] | None = None
    is_error: bool = False
    blocked: bool = False
    blocked_by: str | None = None
    parent_tool_call_id: str | None = None
    tool_results: list[dict[str, Any]] | None = None
    messages: list[dict[str, Any]] | None = None
    error: str | None = None
    error_detail: ErrorDetail | None = None
    end_reason: AgentEndReason | None = None
    response_headers: dict[str, str] | None = None

    cursor_id: str | None = None
    submission_id: str | None = None
    source: SubmissionSource | None = None
    submitter: str | None = None
    correlation: dict[str, Any] | None = None

    purpose: SideCompletionPurpose | None = None
    reason: SideCompletionReason | None = None
    delta: str | None = None
    text: str | None = None
    usage: dict[str, int] | None = None

    attempt: int | None = None
    max_retries: int | None = None
    delay_seconds: float | None = None

    dropped: list[str] | None = None

    suspension: dict[str, Any] | None = None
    resolution: dict[str, Any] | None = None
    close_reason: SuspensionCloseReason | None = None
    read_only: bool | None = None
    lock_holder: dict[str, Any] | None = None


class ChannelSubscriber:
    """An ``ext:`` channel subscription made through an extension's api (#98).

    The bus calls it, which runs ``dispatch``: the handler itself, or its
    policy wrapper. ``api.request`` reads ``handler`` and ``extension`` instead,
    so it runs the bare handler under the requester's terms and names who
    answered.

    Attributes:
        handler: The callable the extension subscribed.
        extension: The subscribing extension's name; ``None`` for an api bound
            to no extension.
        dispatch: What :meth:`EventBus.emit_channel` runs.
    """

    def __init__(
        self, handler: Callable[..., Any], extension: str | None, dispatch: Callable[..., Any]
    ) -> None:
        self.handler = handler
        self.extension = extension
        self.dispatch = dispatch

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        """Run :attr:`dispatch`, as an emit does."""
        return self.dispatch(*args, **kwargs)


@agent_facing(topic="events")
class EventBus:
    """Central event bus for τ-agent-core.

    Provides publish/subscribe for AgentEvents. Used by:
    - AgentSession (for TUI consumers)
    - AgentLoop (for emitting events)
    - Extensions (for event-driven behavior)

    Reference: SUBPHASE-0.0.md, "5. Agent Events" section.
    Reference: PHASE-3-SUBPHASE-0.md EventBus contract.

    Contract:
        class EventBus:
            def on(self, channel: str, handler: Callable) -> Callable[[], None]: ...
            def off(self, channel: str, handler: Callable) -> None: ...
            async def emit(self, event: AgentEvent) -> None: ...
            async def emit_channel(self, channel: str, *args: Any, **kwargs: Any) -> None: ...

    Constraint: "fire-and-forget" is a failure-isolation contract, not a
    scheduling one — see the module docstring. ``emit`` awaits each handler
    that returns a coroutine before moving to the next, so handlers are
    ordered with respect to each other AND to the emitter, and a handler that
    suspends suspends the emitter. Subscribers rely on this to pace the agent
    loop; do not "optimize" it into ``create_task``.

    Attributes:
        _listeners: Dict mapping event type/channel to list of handler callables.
    """

    def __init__(self) -> None:
        self._listeners: dict[str, list[Callable]] = {
            "all": [],
            "agent_start": [],
            "agent_end": [],
            "turn_start": [],
            "turn_end": [],
            "message_start": [],
            "message_update": [],
            "message_end": [],
            "tool_execution_start": [],
            "tool_execution_update": [],
            "tool_execution_end": [],
            "side_completion_start": [],
            "side_completion_update": [],
            "side_completion_end": [],
            "provider_retry": [],
            "follow_ups_dropped": [],
            "suspension_open": [],
            "suspension_closed": [],
            "lock_changed": [],
        }
        self._error_listeners: list[ErrorListener] = []

    def on(self, channel: str, handler: Callable) -> Callable[[], None]:
        """Subscribe to a channel.

        Args:
            channel: Channel name (e.g., 'all', 'agent_start').
            handler: Callable that receives an AgentEvent.

        Returns:
            An unsubscribe function.

        Example:
            >>> bus = EventBus()
            >>> def my_handler(event):
            ...     print(event.type)
            >>> unsub = bus.on('all', my_handler)
            >>> await bus.emit(AgentEvent(type='agent_start', timestamp=0))
            >>> unsub()  # Remove subscription
        """
        if channel not in self._listeners:
            self._listeners[channel] = []
        self._listeners[channel].append(handler)

        def unsubscribe() -> None:
            try:
                self._listeners[channel].remove(handler)
            except ValueError:
                pass  # Already removed

        return unsubscribe

    def on_error(self, listener: ErrorListener) -> Callable[[], None]:
        """Register a listener for exceptions raised by this bus's handlers.

        S44 (anchor G3). A handler that raises is routed here instead of being
        swallowed; the session binds this to the same on_error surface the
        :class:`~tau_agent_core.extensions.runner.ExtensionRunner` uses. With no
        listener bound, the error goes to stderr.

        Args:
            listener: Called with ``(error, channel)``.

        Returns:
            A callable that removes ``listener``; calling it twice is harmless.
        """
        self._error_listeners.append(listener)

        def unsubscribe() -> None:
            try:
                self._error_listeners.remove(listener)
            except ValueError:
                pass  # Already removed

        return unsubscribe

    def _surface_handler_error(self, error: BaseException, channel: str) -> None:
        """Surface a handler exception; never drop it silently (Fail-Early, S44).

        Notifies every registered :meth:`on_error` listener. With none bound the
        error is written to stderr rather than swallowed. A listener that itself
        raises must not abort the emit loop (that would drop the sibling handlers
        the bus is contractually required to still run), so each listener call is
        guarded and its own failure falls back to stderr.
        """
        if self._error_listeners:
            for listener in list(self._error_listeners):
                try:
                    listener(error, channel)
                except Exception as listener_err:  # noqa: BLE001 — reporter must not crash emit
                    print(
                        f"[τ] event-bus error listener failed on {channel!r}: {listener_err}",
                        file=sys.stderr,
                    )
        else:
            print(
                f"[τ] unhandled error in {channel!r} handler: {error}",
                file=sys.stderr,
            )

    def off(self, channel: str, handler: Callable) -> None:
        """Remove a specific handler from a channel.

        Args:
            channel: Channel name.
            handler: The handler to remove.
        """
        if channel in self._listeners:
            try:
                self._listeners[channel].remove(handler)
            except ValueError:
                pass  # Handler not found on this channel

    def has_listeners(self, channel: str) -> bool:
        """Whether anyone is subscribed to ``channel``.

        For a caller deciding whether an un-deliverable emit is a problem: a
        synchronous appender with no running loop cannot dispatch, and that is
        only a lost event if something was waiting for it.

        Args:
            channel: Channel name. ``"all"`` subscribers are not counted — they
                take :meth:`emit`'s ``AgentEvent`` stream, not a named channel.

        Returns:
            True if at least one handler is registered on ``channel``.
        """
        return bool(self._listeners.get(channel))

    def subscribers(self, channel: str) -> tuple[Callable[..., Any], ...]:
        """A snapshot of ``channel``'s handlers, in subscription order.

        Args:
            channel: Channel name; ``"all"`` subscribers are not included.

        Returns:
            The handlers; an api's ``ext:`` subscription is a :class:`ChannelSubscriber`.
        """
        return tuple(self._listeners.get(channel, ()))

    async def emit(self, event: AgentEvent) -> None:
        """Emit an event to all matching handlers.

        Handlers subscribed to the specific event type, then those on 'all',
        receive the event, each awaited in turn. They run outside any hook
        dispatch, so none inherits ``api.suspend``'s permission
        (docs/STEP-DEBUGGER.md §3.2).

        Args:
            event: The AgentEvent to emit.
        """
        with outside_dispatch():
            for channel in (event.type, "all"):
                for handler in list(self._listeners.get(channel, [])):
                    try:
                        result = handler(event)
                        if asyncio.iscoroutine(result):
                            await result
                    except Exception as err:  # noqa: BLE001 — surfaced (S44), not swallowed
                        self._surface_handler_error(err, event.type)

    async def emit_channel(self, channel: str, *args: Any, **kwargs: Any) -> None:
        """Emit to all handlers on a specific channel.

        A handler that raises is surfaced and the rest still run, except for a
        :class:`~tau_agent_core.policies.HandlerPolicyError` (a policed ``ext:``
        handler's ``Failed``), which propagates to the emitter at once. An
        ``ext:`` channel's handlers run in the emitter's hook dispatch, so they
        may ``api.suspend`` where it may; every other channel's run outside it.

        Args:
            channel: Channel name.
            args: Positional arguments passed to each handler.
            kwargs: Keyword arguments passed to each handler.

        Raises:
            HandlerPolicyError: a policed handler failed.
        """
        scope = contextlib.nullcontext() if channel.startswith("ext:") else outside_dispatch()
        with scope:
            for handler in list(self._listeners.get(channel, [])):
                try:
                    result = handler(*args, **kwargs)
                    if asyncio.iscoroutine(result):
                        await result
                except HandlerPolicyError:
                    raise
                except Exception as err:  # noqa: BLE001 — surfaced (S44), not swallowed
                    self._surface_handler_error(err, channel)

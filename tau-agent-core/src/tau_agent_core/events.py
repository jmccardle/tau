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
import sys
from typing import Any, Callable, Literal

from pydantic import BaseModel, Field

from tau_agent_core.submission import SubmissionSource
from tau_llm.docs import agent_facing

ErrorListener = Callable[[BaseException, str], None]

AgentEndReason = Literal[
    "done",
    "terminate",
    "aborted",
    "max_turns",
    "repeat_tool_calls",
    "error",
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
        tool_results: List of tool result messages (turn_end)
        messages: List of messages produced (agent_end)
        error: Why an ``agent_end`` closed, when the loop raised rather than
            finishing (``"RuntimeError: Connection refused"``). ``None`` on a
            normal close; always paired with ``is_error=True``.
        end_reason: How an ``agent_end`` closed, as one of ``"done"``,
            ``"terminate"``, ``"aborted"``, ``"max_turns"``,
            ``"repeat_tool_calls"`` or ``"error"``. ``None`` on every other event
            type. This distinguishes a loop that finished from one that was cut
            short: before it, a run stopped by ``max_turns`` emitted the same
            ``agent_end`` as one where the model simply had nothing more to say,
            so a caller could not tell a truncated answer from a complete one.
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
    tool_results: list[dict[str, Any]] | None = None
    messages: list[dict[str, Any]] | None = None
    error: str | None = None
    end_reason: AgentEndReason | None = None

    submission_id: str | None = None
    source: SubmissionSource | None = None
    submitter: str | None = None
    correlation: dict[str, Any] | None = None

    purpose: SideCompletionPurpose | None = None
    reason: SideCompletionReason | None = None
    delta: str | None = None
    text: str | None = None
    usage: dict[str, int] | None = None


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
        """Register a listener for handler exceptions. Returns an unsubscribe.

        S44 (anchor G3). A notify handler that raises is routed here instead of
        being swallowed — the session binds this to the same on_error surface the
        :class:`~tau_agent_core.extensions.runner.ExtensionRunner` uses, so a
        failing observer is as visible as a failing mutating hook.
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

    async def emit(self, event: AgentEvent) -> None:
        """Emit an event to all matching handlers.

        Handlers subscribed to the specific event type AND to 'all'
        will receive the event. Handlers are called synchronously.
        This is an async method to be compatible with async consumers.

        Args:
            event: The AgentEvent to emit.
        """
        # Call handlers subscribed to the specific event type
        for handler in list(self._listeners.get(event.type, [])):
            try:
                result = handler(event)
                # If handler is a coroutine, run it
                if asyncio.iscoroutine(result):
                    await result
            except Exception as err:  # noqa: BLE001 — surfaced (S44), not swallowed
                self._surface_handler_error(err, event.type)

        # Call handlers subscribed to 'all'
        for handler in list(self._listeners.get("all", [])):
            try:
                result = handler(event)
                if asyncio.iscoroutine(result):
                    await result
            except Exception as err:  # noqa: BLE001 — surfaced (S44), not swallowed
                self._surface_handler_error(err, event.type)

    async def emit_channel(self, channel: str, *args: Any, **kwargs: Any) -> None:
        """Emit to all handlers on a specific channel.

        Args:
            channel: Channel name.
            *args: Positional arguments passed to handlers.
            **kwargs: Keyword arguments passed to handlers.
        """
        for handler in list(self._listeners.get(channel, [])):
            try:
                result = handler(*args, **kwargs)
                if asyncio.iscoroutine(result):
                    await result
            except Exception as err:  # noqa: BLE001 — surfaced (S44), not swallowed
                self._surface_handler_error(err, channel)

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

Constraint: Events are fire-and-forget. Handlers are called synchronously.
"""

from __future__ import annotations

import asyncio
import sys
from typing import Any, Callable, Literal

from pydantic import BaseModel, Field

#: A notify-bus error listener. Called ``listener(exc, channel)`` when a
#: subscribed handler raises — the S44 surface that replaces the old silent
#: ``except Exception: pass``. ``channel`` is the event type / channel the
#: handler was dispatched on, so the listener can attribute the failure.
ErrorListener = Callable[[BaseException, str], None]


class AgentEvent(BaseModel):
    """A single event from the agent loop.

    Reference: SUBPHASE-0.0.md, "5. Agent Events" section.

    Attributes:
        type: Event type discriminator
        timestamp: Milliseconds since epoch
        message: Message data (agent_start/end, message_*)
        turn_index: Turn number (turn_*)
        tool_call_id: Tool call ID (tool_*)
        tool_name: Tool name (tool_*)
        args: Tool execution arguments (tool_execution_start)
        result: Tool execution result (tool_execution_*)
        is_error: Whether this event represents an error
        blocked: Whether a ``tool_execution_end`` is an extension VETO (S50) —
            distinct from a generic errored result, so a front-end can render
            "⛔ blocked by <ext>: <reason>" rather than a plain error box.
        blocked_by: The extension that vetoed the call (its runner path label),
            paired with ``blocked`` on a ``tool_execution_end``; ``None`` otherwise.
        tool_results: List of tool result messages (turn_end)
        messages: List of messages produced (agent_end)
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
    ]

    timestamp: int = Field(ge=0)

    # Conditional fields
    message: dict[str, Any] | None = None
    turn_index: int | None = None
    tool_call_id: str | None = None
    tool_name: str | None = None
    args: dict[str, Any] | None = None
    result: Any | None = None
    is_error: bool = False
    # S50 (roadmap §3, anchor G11): a vetoed tool call is a DISTINCT presentation
    # from a generic errored result. ``blocked`` marks a ``tool_execution_end`` that
    # a `tool_call` extension hook vetoed; ``blocked_by`` names the extension. The
    # ``type`` Literal stays closed (S49) — these are data fields, like ``is_error``.
    blocked: bool = False
    blocked_by: str | None = None
    tool_results: list[dict[str, Any]] | None = None
    messages: list[dict[str, Any]] | None = None


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
            async def emit_channel(self, channel: str, *args, **kwargs) -> None: ...

    Constraint: Events are fire-and-forget. Handlers are called
    synchronously for performance.

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
        }
        # S44 (roadmap §2, anchor G3): a subscribed handler that raises must NOT
        # vanish silently. When set, these listeners receive ``(exc, channel)`` and
        # route the failure to the same on_error surface as the ExtensionRunner
        # (the session installs one that paints a TUI warning / prints a structured
        # headless stderr line). With NO listener the bus still refuses the silent
        # drop and writes to stderr — Fail-Early, never the old ``pass``.
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
                # Fire-and-forget for the SIBLINGS (they must still run), but the
                # failure itself is surfaced through on_error, never dropped.
                self._surface_handler_error(err, event.type)

        # Call handlers subscribed to 'all'
        for handler in list(self._listeners.get("all", [])):
            try:
                result = handler(event)
                if asyncio.iscoroutine(result):
                    await result
            except Exception as err:  # noqa: BLE001 — surfaced (S44), not swallowed
                self._surface_handler_error(err, event.type)

    async def emit_channel(self, channel: str, *args, **kwargs) -> None:
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

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
from typing import Any, Callable, Coroutine, Literal

from pydantic import BaseModel, Field


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
            except Exception:
                pass  # Fail silently — fire-and-forget

        # Call handlers subscribed to 'all'
        for handler in list(self._listeners.get("all", [])):
            try:
                result = handler(event)
                if asyncio.iscoroutine(result):
                    await result
            except Exception:
                pass  # Fail silently — fire-and-forget

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
            except Exception:
                pass  # Fail silently — fire-and-forget

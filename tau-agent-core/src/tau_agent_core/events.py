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

from typing import Any, Literal

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

    Constraint: Events are fire-and-forget. Handlers are called
    synchronously for performance.

    Attributes:
        _listeners: Dict mapping event type to list of handler callables.
    """

    def __init__(self) -> None:
        self._listeners: dict[str, list[callable]] = {
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

    def on(self, event_type: str, handler: callable) -> callable:
        """Subscribe to an event type.

        Args:
            event_type: Event type string (e.g., 'all', 'agent_start').
            handler: Callable that receives an AgentEvent.

        Returns:
            An unsubscribe function.

        Example:
            >>> bus = EventBus()
            >>> def my_handler(event):
            ...     print(event.type)
            >>> unsub = bus.on('all', my_handler)
            >>> bus.emit(AgentEvent(type='agent_start', timestamp=0))
            >>> unsub()  # Remove subscription
        """
        if event_type not in self._listeners:
            self._listeners[event_type] = []
        self._listeners[event_type].append(handler)

        def unsubscribe() -> None:
            try:
                self._listeners[event_type].remove(handler)
            except ValueError:
                pass  # Already removed

        return unsubscribe

    def emit(self, event: AgentEvent) -> None:
        """Emit an event to all matching handlers.

        Handlers subscribed to the specific event type AND to 'all'
        will receive the event. Handlers are called synchronously.

        Args:
            event: The AgentEvent to emit.
        """
        # Call handlers subscribed to the specific event type
        for handler in list(self._listeners.get(event.type, [])):
            try:
                handler(event)
            except Exception:
                pass  # Fail silently — fire-and-forget

        # Call handlers subscribed to 'all'
        for handler in list(self._listeners.get("all", [])):
            try:
                handler(event)
            except Exception:
                pass  # Fail silently — fire-and-forget

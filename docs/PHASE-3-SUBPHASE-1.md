# Phase 3 Subphase 1 — Event Bus

> **Topic**: Implement the async-safe event bus that connects the agent loop to the TUI and extensions.

## Scope

This subphase implements `tau_agent_core.events.EventBus` — the central dispatch mechanism for all agent events. It is the bridge between:
- **Producers**: AgentLoop (emits AgentEvents)
- **Consumers**: TUI widgets, extension handlers

## Reference

- `SUBPHASE-0.0.md` lines 200-220: AgentEvent contract
- `SUBPHASE-3-SUBPHASE-0.md`: EventBus interface
- `docs/extensions.md` lines 1-40: event subscription
- `docs/tau-agent-core.md` lines 400-500: event bus usage

## Implementation Outline

### `tau_agent_core/events.py`

```python
import asyncio
import logging
from typing import Callable, Any

logger = logging.getLogger(__name__)

class EventBus:
    """Async-safe event bus for agent events.

    Producers call emit(). All subscribed handlers are called.
    Handlers are called synchronously for performance. Async handlers
    are wrapped in asyncio.create_task() to avoid blocking.

    Channels:
    - "all": matches every event
    - "agent_start", "agent_end", etc.: exact type match
    - "tool_*": glob matching (optional, implement if needed)
    """

    def __init__(self):
        self._listeners: dict[str, list[Callable]] = {}
        self._global_listeners: list[Callable] = []

    def on(self, channel: str, handler: Callable) -> Callable[[], None]:
        """Subscribe to a channel. Returns unsubscribe function.

        Args:
            channel: Event type (e.g., "agent_start", "tool_execution_end")
                     or "all" for all events.
            handler: Callable[[AgentEvent], Any] — sync or async function

        Returns:
            Callable[[], None] — unsubscribe function
        """
        if channel == "all":
            self._global_listeners.append(handler)
        else:
            if channel not in self._listeners:
                self._listeners[channel] = []
            self._listeners[channel].append(handler)

        def unsubscribe():
            if channel == "all":
                self._global_listeners.remove(handler)
            else:
                self._listeners.get(channel, []).remove(handler)
        return unsubscribe

    def off(self, channel: str, handler: Callable) -> None:
        """Remove a specific handler from a channel."""
        if channel == "all":
            self._global_listeners.remove(handler)
        else:
            self._listeners.get(channel, []).remove(handler)

    async def emit(self, event: AgentEvent) -> None:
        """Emit an event to all listeners.

        Handlers are called in registration order. Errors in handlers
        are logged but do not prevent other handlers from executing.
        Async handlers are awaited. Sync handlers are called directly.
        """
        # Call global listeners
        for handler in list(self._global_listeners):
            await self._call_handler(handler, event)

        # Call type-specific listeners
        for handler in list(self._listeners.get(event.type, [])):
            await self._call_handler(handler, event)

    async def _call_handler(self, handler: Callable, *args) -> None:
        """Call a handler, handling sync/async and errors."""
        try:
            if asyncio.iscoroutinefunction(handler):
                await handler(*args)
            else:
                handler(*args)
        except Exception as e:
            logger.error(f"Event handler error ({args[0].type if args else '?'}): {e}", exc_info=True)
```

### Key Design Decisions

1. **Synchronous handler execution**: Handlers are called synchronously to avoid the overhead of creating tasks. This is critical because the agent loop emits events at high frequency (every token of streaming text).
2. **Async handler support**: If a handler is async (`async def`), it is awaited. This allows extensions to do async work (file I/O, network calls) without blocking.
3. **Error isolation**: Each handler is wrapped in try/except. An error in one handler does not affect other handlers.
4. **Unsubscribe safety**: The unsubscribe function removes the handler from the list. If the handler is being called when unsubscribe is invoked, the handler completes but won't receive future events.

## Done Criteria

- `on()` subscribes a handler and returns an unsubscribe function
- `off()` removes a specific handler from a channel
- `emit()` calls all matching handlers in registration order
- "all" listeners receive every event
- Type-specific listeners receive only matching events
- Async handlers are properly awaited
- Sync handlers are called directly (no task wrapping)
- Errors in handlers are logged but don't crash the bus
- Unsubscribe removes the handler so it won't receive future events
- Multiple subscribers to the same channel all receive events

## Testing Strategy

### Test 1: Subscribe and emit to "all"

```python
async def test_emit_all():
    bus = EventBus()
    received = []
    bus.on("all", lambda e: received.append(e))

    await bus.emit(AgentEvent(type="agent_start"))
    await bus.emit(AgentEvent(type="turn_start", turn_index=0))

    assert len(received) == 2
    assert received[0].type == "agent_start"
    assert received[1].type == "turn_start"
```

### Test 2: Subscribe and emit to specific channel

```python
async def test_emit_specific_channel():
    bus = EventBus()
    received = []
    bus.on("agent_start", lambda e: received.append(e))
    bus.on("all", lambda e: received.append(e))  # also receives all

    await bus.emit(AgentEvent(type="agent_start"))
    await bus.emit(AgentEvent(type="agent_end"))

    # "all" listener received both
    # "agent_start" listener received only agent_start
    all_events = [e for e in received]
    assert len(all_events) == 2
```

### Test 3: Unsubscribe

```python
async def test_unsubscribe():
    bus = EventBus()
    received = []
    unsub = bus.on("all", lambda e: received.append(e))

    await bus.emit(AgentEvent(type="agent_start"))
    assert len(received) == 1

    unsub()

    await bus.emit(AgentEvent(type="agent_end"))
    assert len(received) == 1  # unchanged
```

### Test 4: Async handler

```python
async def test_async_handler():
    bus = EventBus()
    received = []

    async def async_handler(e):
        await asyncio.sleep(0.001)  # simulate async work
        received.append(e)

    bus.on("all", async_handler)
    await bus.emit(AgentEvent(type="agent_start"))
    assert len(received) == 1
```

### Test 5: Error isolation

```python
async def test_error_isolation():
    bus = EventBus()
    received = []
    call_order = []

    def good_handler(e):
        call_order.append("good")
        received.append(e)

    def bad_handler(e):
        call_order.append("bad")
        raise ValueError("boom")

    bus.on("all", good_handler)
    bus.on("all", bad_handler)
    bus.on("all", lambda e: call_order.append("after"))

    await bus.emit(AgentEvent(type="agent_start"))

    # good_handler ran, bad_handler raised, after ran
    assert "good" in call_order
    assert "bad" in call_order
    assert "after" in call_order  # bad handler didn't block
    assert len(received) == 1
```

### Test 6: off() removes specific handler

```python
async def test_off():
    bus = EventBus()
    received = []

    def handler(e):
        received.append(e)

    bus.on("all", handler)
    bus.off("all", handler)

    await bus.emit(AgentEvent(type="agent_start"))
    assert len(received) == 0
```

### Test 7: Handler registration order

```python
async def test_registration_order():
    bus = EventBus()
    order = []

    def handler_a(e):
        order.append("a")

    def handler_b(e):
        order.append("b")

    bus.on("all", handler_a)
    bus.on("all", handler_b)

    await bus.emit(AgentEvent(type="agent_start"))
    assert order == ["a", "b"]  # a was registered first
```

## Success Signal

All 7 test categories pass. The event bus is the central dispatch mechanism. Producers call `emit()` once, and all consumers receive the event. Error handling prevents crashes. Unsubscribe works correctly. Async handlers are awaited.

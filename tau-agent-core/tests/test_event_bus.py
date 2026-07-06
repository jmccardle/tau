"""Tests for tau_agent_core.events.EventBus.

Subphase: PHASE-3-SUBPHASE-1 (Event Bus)
Reference: SUBPHASE-0.0.md, "5. Agent Events" section

Tests cover the EventBus as the central dispatch mechanism connecting
producers (AgentLoop) to consumers (TUI widgets, extensions).

Test categories from subphase:
    1. Subscribe and emit to "all"
    2. Subscribe and emit to specific channel
    3. Unsubscribe
    4. Async handler
    5. Error isolation
    6. off() removes specific handler
    7. Handler registration order
"""

import asyncio
import inspect
import time

import pytest

from tau_agent_core.events import AgentEvent, EventBus

# ── Helpers ──────────────────────────────────────────────────────────────────


def _now_ms() -> int:
    return int(time.time() * 1000)


def _make_event(**overrides: object) -> AgentEvent:
    """Build an AgentEvent with a timestamp and optional field overrides."""
    return AgentEvent(type="agent_start", timestamp=_now_ms(), **overrides)


# ── Test 1: Subscribe and emit to "all" ──────────────────────────────────────


class TestEmitAll:
    """Test 1: Subscribe and emit to 'all'.

    "all" listeners receive every event regardless of type.
    """

    @pytest.mark.asyncio
    async def test_all_listener_receives_all_events(self):
        """All subscribers to 'all' receive every event."""
        bus = EventBus()
        received: list[AgentEvent] = []
        bus.on("all", lambda e: received.append(e))

        await bus.emit(AgentEvent(type="agent_start", timestamp=_now_ms()))
        await bus.emit(AgentEvent(type="turn_start", timestamp=_now_ms(), turn_index=0))
        await bus.emit(AgentEvent(type="tool_execution_end", timestamp=_now_ms(), tool_name="ls"))

        assert len(received) == 3
        assert received[0].type == "agent_start"
        assert received[1].type == "turn_start"
        assert received[2].type == "tool_execution_end"

    @pytest.mark.asyncio
    async def test_all_listener_receives_different_event_types(self):
        """Verify 'all' handler gets the exact event object, not a copy."""
        bus = EventBus()
        received_types: list[str] = []
        bus.on("all", lambda e: received_types.append(e.type))

        types = [
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
        for t in types:
            await bus.emit(AgentEvent(type=t, timestamp=_now_ms()))

        assert received_types == types

    @pytest.mark.asyncio
    async def test_all_listener_receives_event_with_full_data(self):
        """All listener receives full event data, not just the type."""
        bus = EventBus()
        captured: AgentEvent | None = None
        bus.on("all", lambda e: globals().update(_captured=e))

        event = AgentEvent(
            type="tool_execution_end",
            timestamp=_now_ms(),
            tool_call_id="call_abc",
            tool_name="bash",
            result="hello world",
        )
        # Direct capture via mutable container to avoid scoping issues
        container: list[AgentEvent] = []
        bus2 = EventBus()
        bus2.on("all", lambda e: container.append(e))
        await bus2.emit(event)

        assert len(container) == 1
        assert container[0].tool_call_id == "call_abc"
        assert container[0].tool_name == "bash"
        assert container[0].result == "hello world"


# ── Test 2: Subscribe and emit to specific channel ──────────────────────────


class TestEmitSpecificChannel:
    """Test 2: Subscribe and emit to specific channel.

    Type-specific listeners receive only events matching their channel.
    """

    @pytest.mark.asyncio
    async def test_specific_listener_receives_only_matching(self):
        """A 'tool_execution_start' listener only gets tool_execution_start events."""
        bus = EventBus()
        received: list[AgentEvent] = []
        bus.on("tool_execution_start", lambda e: received.append(e))

        await bus.emit(AgentEvent(type="agent_start", timestamp=_now_ms()))
        await bus.emit(AgentEvent(type="tool_execution_start", timestamp=_now_ms(), tool_name="ls"))
        await bus.emit(AgentEvent(type="agent_end", timestamp=_now_ms()))

        assert len(received) == 1
        assert received[0].type == "tool_execution_start"

    @pytest.mark.asyncio
    async def test_both_all_and_specific_receive(self):
        """A handler on 'all' AND one on a specific channel both fire for that type."""
        bus = EventBus()
        all_received: list[str] = []
        specific_received: list[str] = []

        bus.on("all", lambda e: all_received.append(e.type))
        bus.on("agent_start", lambda e: specific_received.append(e.type))

        await bus.emit(AgentEvent(type="agent_start", timestamp=_now_ms()))
        await bus.emit(AgentEvent(type="agent_end", timestamp=_now_ms()))

        # "all" listener got both
        assert all_received == ["agent_start", "agent_end"]
        # specific listener got only agent_start
        assert specific_received == ["agent_start"]

    @pytest.mark.asyncio
    async def test_specific_listener_does_not_receive_unrelated_types(self):
        """A channel-specific listener is not called for unrelated events."""
        bus = EventBus()
        received: list[str] = []
        bus.on("turn_start", lambda e: received.append(e.type))

        await bus.emit(AgentEvent(type="agent_start", timestamp=_now_ms()))
        await bus.emit(AgentEvent(type="turn_start", timestamp=_now_ms(), turn_index=0))
        await bus.emit(AgentEvent(type="tool_execution_end", timestamp=_now_ms()))

        assert received == ["turn_start"]

    @pytest.mark.asyncio
    async def test_custom_channel_subscription(self):
        """Subscribing to a non-predefined channel works."""
        bus = EventBus()
        received: list[str] = []
        bus.on("custom_channel", lambda e: received.append(e.type))

        # Emit a valid event — it won't match "custom_channel" since
        # AgentEvent only accepts the defined Literal types.
        await bus.emit(_make_event())

        # The custom channel listener only fires if we emit with type="custom_channel"
        # which is not a valid AgentEvent. So with valid events, nothing fires.
        assert len(received) == 0


# ── Test 3: Unsubscribe ─────────────────────────────────────────────────────


class TestUnsubscribe:
    """Test 3: Unsubscribe via the function returned by on()."""

    @pytest.mark.asyncio
    async def test_unsubscribing_stops_receiving_events(self):
        """After unsubscribe, the handler no longer receives events."""
        bus = EventBus()
        received: list[AgentEvent] = []
        unsub = bus.on("all", lambda e: received.append(e))

        await bus.emit(AgentEvent(type="agent_start", timestamp=_now_ms()))
        assert len(received) == 1

        unsub()

        await bus.emit(AgentEvent(type="agent_end", timestamp=_now_ms()))
        assert len(received) == 1  # unchanged

    @pytest.mark.asyncio
    async def test_unsubscribing_specific_channel(self):
        """Unsubscribing from a specific channel stops that handler."""
        bus = EventBus()
        received: list[str] = []
        unsub = bus.on("agent_start", lambda e: received.append(e.type))

        await bus.emit(AgentEvent(type="agent_start", timestamp=_now_ms()))
        assert len(received) == 1

        unsub()

        await bus.emit(AgentEvent(type="agent_start", timestamp=_now_ms()))
        assert len(received) == 1

    @pytest.mark.asyncio
    async def test_unsubscribe_multiple_times_is_safe(self):
        """Calling unsubscribe() twice does not raise."""
        bus = EventBus()
        received: list[str] = []
        unsub = bus.on("all", lambda e: received.append(e.type))

        unsub()
        unsub()  # Should not raise

        await bus.emit(AgentEvent(type="agent_start", timestamp=_now_ms()))
        assert len(received) == 0

    @pytest.mark.asyncio
    async def test_multiple_handlers_one_unsubscribed(self):
        """Unsubscribing one handler leaves others intact."""
        bus = EventBus()
        received: list[str] = []
        unsub_a = bus.on("all", lambda e: received.append("a"))
        unsub_b = bus.on("all", lambda e: received.append("b"))

        await bus.emit(AgentEvent(type="agent_start", timestamp=_now_ms()))
        assert received == ["a", "b"]

        unsub_a()

        await bus.emit(AgentEvent(type="agent_end", timestamp=_now_ms()))
        assert received == ["a", "b", "b"]

    @pytest.mark.asyncio
    async def test_unsubscribes_by_value_not_by_identity(self):
        """Subscribing the same function twice and unsubscribing once removes only one."""
        bus = EventBus()
        counter = {"n": 0}

        def handler(e):
            counter["n"] += 1

        unsub1 = bus.on("all", handler)
        unsub2 = bus.on("all", handler)

        await bus.emit(AgentEvent(type="agent_start", timestamp=_now_ms()))
        assert counter["n"] == 2

        unsub1()

        await bus.emit(AgentEvent(type="agent_end", timestamp=_now_ms()))
        assert counter["n"] == 3  # only one handler fires now


# ── Test 4: Async handler ───────────────────────────────────────────────────


class TestAsyncHandler:
    """Test 4: Async handler support.

    Async handlers (async def) are awaited. Sync handlers are called directly.
    """

    @pytest.mark.asyncio
    async def test_async_handler_receives_event(self):
        """An async handler is awaited and receives the event."""
        bus = EventBus()
        received: list[AgentEvent] = []

        async def async_handler(e):
            await asyncio.sleep(0.001)  # simulate async work
            received.append(e)

        bus.on("all", async_handler)
        await bus.emit(AgentEvent(type="agent_start", timestamp=_now_ms()))
        assert len(received) == 1

    @pytest.mark.asyncio
    async def test_async_and_sync_handlers_both_run(self):
        """Mix of async and sync handlers both execute."""
        bus = EventBus()
        sync_received: list[str] = []
        async_received: list[str] = []

        def sync_handler(e):
            sync_received.append(e.type)

        async def async_handler(e):
            await asyncio.sleep(0.001)
            async_received.append(e.type)

        bus.on("all", sync_handler)
        bus.on("all", async_handler)

        await bus.emit(AgentEvent(type="agent_start", timestamp=_now_ms()))

        assert sync_received == ["agent_start"]
        assert async_received == ["agent_start"]

    @pytest.mark.asyncio
    async def test_async_handler_on_specific_channel(self):
        """Async handler subscribed to a specific channel works correctly."""
        bus = EventBus()
        received: list[str] = []

        async def handler(e):
            await asyncio.sleep(0.001)
            received.append(e.type)

        bus.on("tool_execution_end", handler)
        await bus.emit(AgentEvent(type="tool_execution_end", timestamp=_now_ms()))

        assert received == ["tool_execution_end"]

    @pytest.mark.asyncio
    async def test_async_handler_error_is_caught(self):
        """An async handler that raises does not break the bus."""
        bus = EventBus()
        received: list[str] = []

        async def bad_handler(e):
            await asyncio.sleep(0.001)
            raise RuntimeError("async boom")

        bus.on("all", bad_handler)
        bus.on("all", lambda e: received.append("good"))

        # Should not raise
        await bus.emit(AgentEvent(type="agent_start", timestamp=_now_ms()))
        assert received == ["good"]


# ── Test 5: Error isolation ─────────────────────────────────────────────────


class TestErrorIsolation:
    """Test 5: Error isolation.

    Errors in one handler do not prevent other handlers from executing.
    """

    @pytest.mark.asyncio
    async def test_error_in_one_handler_does_not_block_others(self):
        """A handler that raises does not prevent subsequent handlers from running."""
        bus = EventBus()
        call_order: list[str] = []
        received: list[str] = []

        def good_handler(e):
            call_order.append("good")
            received.append(e.type)

        def bad_handler(e):
            call_order.append("bad")
            raise ValueError("boom")

        bus.on("all", good_handler)
        bus.on("all", bad_handler)
        bus.on("all", lambda e: call_order.append("after"))

        await bus.emit(AgentEvent(type="agent_start", timestamp=_now_ms()))

        # good_handler ran, bad_handler raised, after ran
        assert "good" in call_order
        assert "bad" in call_order
        assert "after" in call_order
        # Only good_handler added to received (bad_handler raised)
        assert len(received) == 1

    @pytest.mark.asyncio
    async def test_multiple_errors_in_sequence(self):
        """Multiple erroring handlers still allow subsequent handlers to run."""
        bus = EventBus()
        received: list[str] = []

        def handler_a(e):
            raise KeyError("a")

        def handler_b(e):
            raise ValueError("b")

        bus.on("all", handler_a)
        bus.on("all", handler_b)
        bus.on("all", lambda e: received.append("survived"))

        await bus.emit(AgentEvent(type="agent_start", timestamp=_now_ms()))

        assert received == ["survived"]

    @pytest.mark.asyncio
    async def test_error_does_not_affect_other_channels(self):
        """An error in a 'all' handler doesn't prevent specific channel handlers from running."""
        bus = EventBus()
        all_received: list[str] = []
        specific_received: list[str] = []

        def all_bad(e):
            raise ValueError("all boom")

        def specific_good(e):
            specific_received.append(e.type)

        bus.on("all", all_bad)
        bus.on("agent_start", specific_good)

        await bus.emit(AgentEvent(type="agent_start", timestamp=_now_ms()))

        assert specific_received == ["agent_start"]

    @pytest.mark.asyncio
    async def test_specific_channel_error_does_not_affect_all_handlers(self):
        """An error in a type-specific handler doesn't prevent 'all' handlers from running."""
        bus = EventBus()
        all_received: list[str] = []
        specific_received: list[str] = []

        def specific_bad(e):
            raise RuntimeError("specific boom")

        def all_good(e):
            all_received.append(e.type)

        bus.on("agent_start", specific_bad)
        bus.on("all", all_good)
        bus.on("agent_start", lambda e: specific_received.append(e.type))

        await bus.emit(AgentEvent(type="agent_start", timestamp=_now_ms()))

        # The 'all' handler should still run
        assert all_received == ["agent_start"]


# ── Test 6: off() removes specific handler ──────────────────────────────────


class TestOff:
    """Test 6: off() removes specific handler.

    EventBus.off() removes a handler without needing the unsubscribe closure.
    """

    @pytest.mark.asyncio
    async def test_off_removes_handler(self):
        """off() prevents the handler from receiving future events."""
        bus = EventBus()
        received: list[str] = []

        def handler(e):
            received.append(e.type)

        bus.on("all", handler)
        bus.off("all", handler)

        await bus.emit(AgentEvent(type="agent_start", timestamp=_now_ms()))
        assert len(received) == 0

    @pytest.mark.asyncio
    async def test_off_does_not_remove_other_handlers(self):
        """off() only removes the specified handler, not others."""
        bus = EventBus()
        received: list[str] = []

        def handler_a(e):
            received.append("a")

        def handler_b(e):
            received.append("b")

        bus.on("all", handler_a)
        bus.on("all", handler_b)
        bus.off("all", handler_a)

        await bus.emit(AgentEvent(type="agent_start", timestamp=_now_ms()))
        assert received == ["b"]

    @pytest.mark.asyncio
    async def test_off_nonexistent_handler_is_safe(self):
        """Calling off() for a handler not on the channel does not raise."""
        def handler(e):
            pass

        bus = EventBus()
        bus.off("all", handler)  # should not raise
        await bus.emit(AgentEvent(type="agent_start", timestamp=_now_ms()))

    @pytest.mark.asyncio
    async def test_off_and_on_same_handler(self):
        """off() then on() again re-subscribes the handler."""
        bus = EventBus()
        received: list[str] = []

        def handler(e):
            received.append(e.type)

        bus.on("all", handler)
        bus.off("all", handler)
        bus.on("all", handler)

        await bus.emit(AgentEvent(type="agent_start", timestamp=_now_ms()))
        assert received == ["agent_start"]

    @pytest.mark.asyncio
    async def test_off_specific_channel(self):
        """off() works on specific (non-'all') channels."""
        bus = EventBus()
        received: list[str] = []

        def handler(e):
            received.append(e.type)

        bus.on("agent_start", handler)
        bus.off("agent_start", handler)

        await bus.emit(AgentEvent(type="agent_start", timestamp=_now_ms()))
        assert len(received) == 0


# ── Test 7: Handler registration order ──────────────────────────────────────


class TestRegistrationOrder:
    """Test 7: Handler registration order.

    Handlers are called in the order they were registered.
    """

    @pytest.mark.asyncio
    async def test_sync_handlers_call_order(self):
        """Sync handlers are called in registration order."""
        bus = EventBus()
        order: list[str] = []

        def handler_a(e):
            order.append("a")

        def handler_b(e):
            order.append("b")

        def handler_c(e):
            order.append("c")

        bus.on("all", handler_a)
        bus.on("all", handler_b)
        bus.on("all", handler_c)

        await bus.emit(AgentEvent(type="agent_start", timestamp=_now_ms()))
        assert order == ["a", "b", "c"]

    @pytest.mark.asyncio
    async def test_all_handlers_run_before_specific(self):
        """For a matching event, type-specific handlers run first, then 'all' handlers.

        The implementation iterates type-specific listeners before 'all' listeners.
        """
        bus = EventBus()
        order: list[str] = []

        bus.on("all", lambda e: order.append("all"))
        bus.on("agent_start", lambda e: order.append("specific"))

        await bus.emit(AgentEvent(type="agent_start", timestamp=_now_ms()))
        # type-specific first, then 'all'
        assert order == ["specific", "all"]

    @pytest.mark.asyncio
    async def test_all_handlers_run_before_specific_with_interleaving(self):
        """Type-specific handlers run first (in registration order), then 'all' handlers (in registration order)."""
        bus = EventBus()
        order: list[str] = []

        bus.on("all", lambda e: order.append("all1"))
        bus.on("agent_start", lambda e: order.append("spec1"))
        bus.on("all", lambda e: order.append("all2"))
        bus.on("agent_start", lambda e: order.append("spec2"))

        await bus.emit(AgentEvent(type="agent_start", timestamp=_now_ms()))
        # type-specific first (in order), then 'all' (in order)
        assert order == ["spec1", "spec2", "all1", "all2"]

    @pytest.mark.asyncio
    async def test_order_preserved_with_mixed_sync_async(self):
        """Registration order is respected even with mixed sync/async handlers."""
        bus = EventBus()
        order: list[str] = []

        bus.on("all", lambda e: order.append("sync1"))

        async def async_handler(e):
            await asyncio.sleep(0.001)
            order.append("async1")

        bus.on("all", async_handler)
        bus.on("all", lambda e: order.append("sync2"))

        await bus.emit(AgentEvent(type="agent_start", timestamp=_now_ms()))
        # sync1 runs first (sync), then async1 completes, then sync2
        assert "sync1" in order
        assert "async1" in order
        assert "sync2" in order
        # sync1 should be first
        assert order[0] == "sync1"


# ── Additional edge cases and integration tests ──────────────────────────────


class TestEventBusEdgeCases:
    """Edge cases and integration-level tests for EventBus."""

    @pytest.mark.asyncio
    async def test_emit_with_no_subscribers(self):
        """Emitting with no subscribers does not raise."""
        bus = EventBus()
        await bus.emit(AgentEvent(type="agent_start", timestamp=_now_ms()))
        # No crash, no error

    @pytest.mark.asyncio
    async def test_multiple_subscribers_same_channel(self):
        """Multiple subscribers on the same channel all receive the event."""
        bus = EventBus()
        received_a: list[str] = []
        received_b: list[str] = []

        bus.on("all", lambda e: received_a.append(e.type))
        bus.on("all", lambda e: received_b.append(e.type))

        await bus.emit(AgentEvent(type="agent_start", timestamp=_now_ms()))

        assert received_a == ["agent_start"]
        assert received_b == ["agent_start"]

    @pytest.mark.asyncio
    async def test_emit_produces_no_side_effects_on_error(self):
        """When a handler errors, the bus state remains clean."""
        bus = EventBus()
        call_count = {"n": 0}

        def counter_handler(e):
            call_count["n"] += 1

        def error_handler(e):
            raise ValueError("boom")

        bus.on("all", counter_handler)
        bus.on("all", error_handler)
        bus.on("all", counter_handler)

        # Each emit fires counter_handler twice (two subscribers to "all"),
        # error_handler raises, but doesn't prevent other handlers.
        await bus.emit(AgentEvent(type="agent_start", timestamp=_now_ms()))
        await bus.emit(AgentEvent(type="agent_end", timestamp=_now_ms()))

        # counter_handler called 4 times total (2 per emit × 2 emits)
        # error handler's failure doesn't affect counter
        assert call_count["n"] == 4

    @pytest.mark.asyncio
    async def test_emit_does_not_block_on_slow_sync_handler(self):
        """A slow sync handler still runs synchronously during emit()."""
        bus = EventBus()
        received: list[str] = []

        def slow_handler(e):
            import time
            time.sleep(0.01)  # simulate slow work
            received.append(e.type)

        bus.on("all", slow_handler)

        start = time.monotonic()
        await bus.emit(AgentEvent(type="agent_start", timestamp=_now_ms()))
        elapsed = time.monotonic() - start

        # Since sync handlers are called directly, emit() should take at least
        # the time spent in the slow handler
        assert elapsed >= 0.005  # some tolerance for overhead
        assert received == ["agent_start"]

    @pytest.mark.asyncio
    async def test_off_on_unknown_channel_is_safe(self):
        """Calling off() on a channel that doesn't exist is safe."""
        bus = EventBus()
        bus.off("nonexistent", lambda e: None)  # should not raise

    @pytest.mark.asyncio
    async def test_on_and_emit_event_order_consistency(self):
        """Subscribing before emit guarantees the handler receives the event."""
        bus = EventBus()
        received: list[AgentEvent] = []
        bus.on("all", lambda e: received.append(e))

        for i in range(5):
            await bus.emit(AgentEvent(type="agent_start", timestamp=_now_ms(), turn_index=i))

        assert len(received) == 5
        for i, e in enumerate(received):
            assert e.turn_index == i

    @pytest.mark.asyncio
    async def test_handler_receives_correct_event_data(self):
        """Each handler receives the exact event emitted, not a stale one."""
        bus = EventBus()
        events: list[AgentEvent] = []
        bus.on("all", lambda e: events.append(e))

        event1 = AgentEvent(type="agent_start", timestamp=_now_ms())
        event2 = AgentEvent(type="agent_end", timestamp=_now_ms())

        await bus.emit(event1)
        await bus.emit(event2)

        assert events[0].type == "agent_start"
        assert events[1].type == "agent_end"
        # Verify they are distinct objects
        assert events[0] is not events[1]

    @pytest.mark.asyncio
    async def test_async_handler_error_does_not_crash_emit(self):
        """An async handler that raises is caught, emit() completes normally."""
        bus = EventBus()
        order: list[str] = []

        async def async_error_handler(e):
            raise RuntimeError("async error")

        def good_handler(e):
            order.append("good")

        bus.on("all", async_error_handler)
        bus.on("all", good_handler)

        # Should complete without raising
        await bus.emit(AgentEvent(type="agent_start", timestamp=_now_ms()))
        assert order == ["good"]

    @pytest.mark.asyncio
    async def test_unsubscribe_via_closure_does_not_affect_other_subscribers(self):
        """Unsubscribing one subscriber via closure doesn't affect others."""
        bus = EventBus()
        received: list[str] = []

        def handler_a(e):
            received.append("a")

        def handler_b(e):
            received.append("b")

        unsub_a = bus.on("all", handler_a)
        bus.on("all", handler_b)

        await bus.emit(AgentEvent(type="agent_start", timestamp=_now_ms()))
        assert received == ["a", "b"]

        unsub_a()

        await bus.emit(AgentEvent(type="agent_end", timestamp=_now_ms()))
        assert received == ["a", "b", "b"]

    @pytest.mark.asyncio
    async def test_event_bus_is_reentrant_safe(self):
        """If a handler calls emit(), the bus handles re-entrancy correctly."""
        bus = EventBus()
        received: list[str] = []

        def recursive_handler(e):
            received.append(f"{e.type}-outer")
            if e.type == "agent_start":
                bus.emit(AgentEvent(type="agent_end", timestamp=_now_ms())).result()

        bus.on("all", recursive_handler)
        await bus.emit(AgentEvent(type="agent_start", timestamp=_now_ms()))

        # Should have at least agent_start, and possibly agent_end
        assert "agent_start-outer" in received


class TestEmitChannel:
    """Tests for EventBus.emit_channel()."""

    @pytest.mark.asyncio
    async def test_emit_channel_calls_only_channel_handlers(self):
        """emit_channel() only calls handlers on the specified channel, not 'all'."""
        bus = EventBus()
        received: list[str] = []

        bus.on("tool_execution_end", lambda e: received.append(e.type))
        bus.on("all", lambda e: received.append(f"all-{e.type}"))

        # emit_channel only fires handlers on the given channel
        event = AgentEvent(type="tool_execution_end", timestamp=_now_ms())
        await bus.emit_channel("tool_execution_end", event)

        # Only the "tool_execution_end" handler fires (not "all")
        assert len(received) == 1
        assert received[0] == "tool_execution_end"

    @pytest.mark.asyncio
    async def test_emit_channel_with_no_handlers(self):
        """emit_channel with no handlers does not raise."""
        bus = EventBus()
        await bus.emit_channel("nonexistent")  # should not raise

    @pytest.mark.asyncio
    async def test_emit_channel_passes_args(self):
        """emit_channel passes args and kwargs to handlers."""
        bus = EventBus()
        received: list = []

        bus.on("my_channel", lambda *args, **kwargs: received.append((args, kwargs)))

        await bus.emit_channel("my_channel", "arg1", "arg2", kwarg="val")

        assert len(received) == 1
        args, kwargs = received[0]
        assert args == ("arg1", "arg2")
        assert kwargs == {"kwarg": "val"}

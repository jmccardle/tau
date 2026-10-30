"""What a frontend watches (D15): live events from the provider, saved events from the driver.

A provider's `sample` stream yields deltas (no cursor: the provider does not know which
cursor it serves). The driver wraps each as a `Live` naming the cursor, and after applying
each answer to core emits a `Saved` naming it. `Driver.events()` hands a frontend an
`EventStream` of both.
"""

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from types import TracebackType


@dataclass(frozen=True)
class TextDelta:
    """Text of the reply arriving."""

    text: str


@dataclass(frozen=True)
class ThinkingDelta:
    """Thinking text arriving."""

    text: str


@dataclass(frozen=True)
class CallNamed:
    """A call begins forming: call number `index` in this reply, with its `id` and tool."""

    index: int
    id: str
    tool: str


@dataclass(frozen=True)
class CallArgs:
    """More of call `index`'s argument text (`args_raw`)."""

    index: int
    text: str


Delta = TextDelta | ThinkingDelta | CallNamed | CallArgs
DELTAS = (TextDelta, ThinkingDelta, CallNamed, CallArgs)


@dataclass(frozen=True)
class Live:
    """A delta, on the cursor whose sample produced it. Never saved: a frontend draws it as
    a layer over the cursor and drops the layer when a `Saved` arrives."""

    cursor: int
    delta: Delta


@dataclass(frozen=True)
class Saved:
    """The tree changed for this cursor: look at it."""

    cursor: int


Event = Live | Saved


class EventStream:
    """One subscriber's events: an async iterator with its own bounded buffer.

    A `Live` that arrives when the buffer is full is dropped. A `Saved` never is: it is
    merged into an equal `Saved` just before it, or takes the place of the oldest `Live` in
    the buffer; only when the buffer holds nothing but `Saved` events does it grow past
    `capacity`. `aclose()` unsubscribes; a driver that stops ends the stream after what it
    holds.
    """

    def __init__(
        self, cursor: int | None, capacity: int, release: Callable[["EventStream"], None]
    ) -> None:
        if capacity < 1:
            raise ValueError(f"an event buffer holds at least one event, not {capacity}")
        self.cursor = cursor
        self.capacity = capacity
        self._release = release
        self._buffer: list[Event] = []
        self._ended = False
        self._waiter: asyncio.Future[None] | None = None

    def offer(self, event: Event) -> None:
        """Called by the driver. Never waits, never raises."""
        if self._ended or (self.cursor is not None and event.cursor != self.cursor):
            return
        if isinstance(event, Saved):
            if self._buffer and self._buffer[-1] == event:
                return
            if len(self._buffer) >= self.capacity:
                self._evict_live()
                if self._buffer and self._buffer[-1] == event:
                    return
        elif len(self._buffer) >= self.capacity:
            return
        self._buffer.append(event)
        self._wake()

    def _evict_live(self) -> None:
        for i, held in enumerate(self._buffer):
            if isinstance(held, Live):
                del self._buffer[i]
                if 0 < i < len(self._buffer) and self._buffer[i - 1] == self._buffer[i]:
                    del self._buffer[i]  # two equal `Saved` now touch
                return

    def _wake(self) -> None:
        if self._waiter is not None and not self._waiter.done():
            self._waiter.set_result(None)

    def end(self) -> None:
        """No more events will arrive; what is buffered is still delivered."""
        self._ended = True
        self._wake()

    async def aclose(self) -> None:
        """Unsubscribe and drop what is buffered."""
        self._buffer.clear()
        self.end()
        self._release(self)

    def __aiter__(self) -> "EventStream":
        return self

    async def __anext__(self) -> Event:
        while True:
            if self._buffer:
                return self._buffer.pop(0)
            if self._ended:
                raise StopAsyncIteration
            self._waiter = asyncio.get_running_loop().create_future()
            try:
                await self._waiter
            finally:
                self._waiter = None

    async def __aenter__(self) -> "EventStream":
        return self

    async def __aexit__(
        self,
        kind: type[BaseException] | None,
        error: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()

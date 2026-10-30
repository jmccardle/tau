"""What a frontend watches (D15): live events from providers and tools, saved ones from the driver.

A provider's `sample` stream yields deltas (no cursor: the provider does not know which
cursor it serves), and a running tool reports `ToolProgress` through its `progress` callback.
The driver wraps each as a `Live` naming the cursor, and after applying each answer to core
emits a `Saved` naming it. `Driver.events()` hands a frontend an
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


@dataclass(frozen=True)
class ToolProgress:
    """A running call showing partial output: `text` is new output since the last
    `ToolProgress` of the call `call_id`, not the whole so far. Only the driver makes one (a
    tool calls its `progress`); a provider that yields one fails its sample. What the call
    saves is its `Outcome`, never this."""

    call_id: str
    text: str


Delta = TextDelta | ThinkingDelta | CallNamed | CallArgs | ToolProgress
DELTAS = (TextDelta, ThinkingDelta, CallNamed, CallArgs)  # what a provider may yield


@dataclass(frozen=True)
class Live:
    """A delta, on the cursor whose sample or tool call produced it. Never saved: a frontend
    draws it as a layer over the cursor and drops the layer when a `Saved` arrives."""

    cursor: int
    delta: Delta


@dataclass(frozen=True)
class Saved:
    """The tree changed for this cursor: look at it."""

    cursor: int


Event = Live | Saved


class EventStream:
    """One subscriber's events: an async iterator with its own bounded buffer, for a single
    consumer (a second concurrent `anext` raises `RuntimeError`).

    A `Live` that arrives when the buffer holds `capacity` events is dropped. A `Saved(c)`
    is never dropped: it first removes every `Live` for cursor `c` and any earlier
    `Saved(c)` in the buffer (the frontend drops that cursor's live layer at `Saved(c)`
    anyway, and "look at the tree" needs saying once), then joins the end. So the buffer
    never holds more than `capacity` plus one `Saved` per cursor.

    `aclose()` unsubscribes. The driver holds streams weakly, so one abandoned without
    `aclose()` is collected. When the driver stops the stream ends after what it holds;
    when the driver fails, `anext` raises the failure after what it holds.
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
        self._failure: Exception | None = None
        self._waiter: asyncio.Future[None] | None = None

    def offer(self, event: Event) -> None:
        """Called by the driver. Never waits, never raises."""
        if self._ended or (self.cursor is not None and event.cursor != self.cursor):
            return
        if isinstance(event, Saved):
            self._buffer = [
                held
                for held in self._buffer
                if held.cursor != event.cursor  # this cursor's `Live`s and earlier `Saved`
            ]
        elif len(self._buffer) >= self.capacity:
            return
        self._buffer.append(event)
        self._wake()

    def _wake(self) -> None:
        if self._waiter is not None and not self._waiter.done():
            self._waiter.set_result(None)

    def end(self, failure: Exception | None = None) -> None:
        """No more events will arrive; what is buffered is still delivered, then `anext`
        raises `failure` (or ends, if there is none)."""
        self._ended = True
        self._failure = failure
        self._wake()

    async def aclose(self) -> None:
        """Unsubscribe and drop what is buffered."""
        self._buffer.clear()
        self._failure = None
        self.end()
        self._release(self)

    def __aiter__(self) -> "EventStream":
        return self

    async def __anext__(self) -> Event:
        while True:
            if self._buffer:
                return self._buffer.pop(0)
            if self._ended:
                if self._failure is not None:
                    raise self._failure
                raise StopAsyncIteration
            if self._waiter is not None:
                raise RuntimeError("an EventStream has one consumer; another is waiting on it")
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

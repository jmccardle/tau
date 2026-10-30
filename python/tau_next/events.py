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
    `ToolProgress` of the same call, not the whole so far. The call is named by `node`, the
    batch's node (the `Assistant` node holding the call), and `call_id`: a server may reuse
    ids across batches, so the id alone does not say which call. Only the driver makes one (a
    tool calls its `progress`); a provider cannot yield one. What the call saves is its
    `Outcome`, never this."""

    node: int
    call_id: str
    text: str


Delta = TextDelta | ThinkingDelta | CallNamed | CallArgs
"""What a provider yields while a part is still arriving."""
LiveDelta = Delta | ToolProgress
"""What a `Live` carries: a provider's delta, or a tool's progress."""
CallKey = tuple[int, str]
"""A call in a `Saved`: `(node, call_id)`, as `ToolProgress` names it."""


@dataclass(frozen=True)
class Live:
    """A delta, on the cursor whose sample or tool call produced it. Never saved: a frontend
    draws it as a layer over the cursor and drops the layer when a `Saved` arrives."""

    cursor: int
    delta: LiveDelta


@dataclass(frozen=True)
class Saved:
    """The tree changed for this cursor: look at it. `calls` are the calls, as `(node, call_id)`,
    whose results this save put in the tree (the `Results` node it added answers them): a
    frontend drops their `ToolProgress` layers now, not before. `reply` is true only when the
    answer saved was a sample's (`Answer.Sampled`): the reply is in the tree, so the cursor's
    live provider deltas are now stale. Any other `Saved` (typing ahead, a hook's write, a
    tool result) leaves the sample streaming, and its deltas still unread-worthy."""

    cursor: int
    calls: tuple[CallKey, ...] = ()
    reply: bool = False


Event = Live | Saved


class EventStream:
    """One subscriber's events: an async iterator with its own bounded buffer, for a single
    consumer (a second concurrent `anext` raises `RuntimeError`).

    A `Live` that arrives when the buffer holds `capacity` events is dropped, tool progress
    included. A `Saved(c)` is never dropped: it first removes any earlier `Saved(c)` ("look at
    the tree" needs saying once) and, only if it is a `Saved(c, reply=True)`, the provider
    deltas (text, thinking, call) buffered for cursor `c`, then joins the end, carrying the
    `calls` of the `Saved(c)` it replaced and the OR of their `reply`. Buffered
    `ToolProgress` of `c` survives it, except the progress of the calls in `Saved.calls`:
    those calls' results are now in the tree. The bound: at most `capacity` events, plus one
    `Saved` per cursor. A full buffer drops any `Live`, progress included, so progress that
    survives clears cannot grow it.

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
            event = self._merged(event)
            self._buffer = [held for held in self._buffer if self._survives(held, event)]
        elif len(self._buffer) >= self.capacity:
            return
        self._buffer.append(event)
        self._wake()

    def _merged(self, saved: Saved) -> Saved:
        """`saved`, carrying the `calls` of the buffered `Saved` of its cursor and their `reply`:
        it replaces them."""
        earlier = [h for h in self._buffer if isinstance(h, Saved) and h.cursor == saved.cursor]
        calls = tuple(
            dict.fromkeys(c for held in (*(h.calls for h in earlier), saved.calls) for c in held)
        )
        return Saved(saved.cursor, calls, saved.reply or any(h.reply for h in earlier))

    @staticmethod
    def _survives(held: Event, saved: Saved) -> bool:
        """Whether a buffered event outlives `saved`: another cursor's does; this cursor's
        `Saved` does not; its `Live` stays unless `saved` is a reply (deltas) or answers the
        call (progress)."""
        if held.cursor != saved.cursor:
            return True
        if not isinstance(held, Live):
            return False
        if isinstance(held.delta, ToolProgress):
            return (held.delta.node, held.delta.call_id) not in saved.calls
        return not saved.reply

    def _wake(self) -> None:
        if self._waiter is not None and not self._waiter.done():
            self._waiter.set_result(None)

    def end(self, failure: Exception | None = None) -> None:
        """No more events will arrive; what is buffered is still delivered, then `anext`
        raises `failure` (or ends, if there is none). A stream that has ended stays as it ended:
        a driver that failed and is then stopped does not turn the failure into a clean end."""
        if self._ended:
            return
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

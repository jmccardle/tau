"""The driver's clients: who is connected, and what each is sent (D16 section 5).

`Hub.sync` is the one place state events are made: it compares the session (and the driver's
questions and step endings) with what it last published and sends the difference, so nothing
is announced by hand and nothing is announced twice.
"""

import asyncio
import weakref
from collections.abc import Callable, Collection, Sequence
from types import TracebackType

from tau._core import Ending, Session
from tau._events import (
    Appended,
    CursorGone,
    CursorView,
    CutOff,
    Event,
    HookFailure,
    Labels,
    Live,
    LiveDelta,
    Notice,
    OpenQuestion,
)


class Feed:
    """One client's stream: an async iterator of `Event` with its own bounded buffer, for a
    single consumer (a second concurrent `anext` raises `RuntimeError`).

    The stream keeps the order things happened in. `Labels` and a `CursorView` replace their own
    earlier copy, and a `CursorGone` its cursor's `CursorView`, only if nothing but state events
    follows the held copy; a `CursorView` or `CursorGone` then takes its slot, a `Labels` goes to
    the end (after the cursors it names). Otherwise the new one is appended after everything
    and the held one stays. Moving it to the end instead would drop a cursor from the snapshot
    (which ends at its `Labels`), and keeping the slot would put a `CursorView` before the
    `Appended` nodes it points at and before the `Live` deltas of its step.

    `Live` is heard only for the cursors being `watching`, and dropped when `capacity` are
    waiting. `Appended` and `Notice` are never dropped: a backlog of `capacity` of them cuts the
    client off (the stream ends with `CutOff`, nothing more is delivered). The bound
    is `capacity` of each of the first two classes, plus, in each run of state events between
    them, one per cursor and one `Labels`.

    `close()` disconnects. The hub holds feeds weakly, so one abandoned without `close()` is
    collected and its client is no longer connected. When the driver stops the stream ends
    after what it holds. Every other end is a `ConnectionError` after what it holds: `CutOff`,
    or, when the driver fails, one whose message is the failure's `"Type: msg"`, chained to it.
    """

    def __init__(
        self,
        client: str,
        capacity: int,
        release: Callable[["Feed"], None],
    ) -> None:
        if capacity < 1:
            raise ValueError(f"an event buffer holds at least one event, not {capacity}")
        self.client = client
        self.capacity = capacity
        self.watching: frozenset[int] = frozenset()
        self._release = release
        self._buffer: list[Event] = []
        self._lives = 0
        self._backlog = 0  # Appended and Notice waiting
        self._ended = False
        self._failure: Exception | None = None
        self._waiter: asyncio.Future[None] | None = None

    def offer(self, event: Event) -> None:
        """Called by the hub. Never waits, never raises."""
        if self._ended:
            return
        match event:
            case Live(cursor=cursor):
                if cursor not in self.watching or self._lives >= self.capacity:
                    return
                self._lives += 1
            case Appended() | Notice():
                if self._backlog >= self.capacity:
                    self._cut_off()
                    return
                self._backlog += 1
            case _:
                self._replace(event)
                return
        self._buffer.append(event)
        self._wake()

    def _replace(self, event: Event) -> None:
        """Queue the state event `event` in place of its last held copy if only state events
        follow that copy (`Labels` at the end, the rest in its slot), else after everything."""
        buffer = self._buffer
        copies = [i for i, held in enumerate(buffer) if _replaces(event, held)]
        if copies and all(_is_state(held) for held in buffer[copies[-1] + 1 :]):
            held = copies[-1]
            if isinstance(event, Labels):
                del buffer[held]
                buffer.append(event)
            else:
                buffer[held] = event
        else:
            buffer.append(event)
        self._wake()

    def _cut_off(self) -> None:
        self._buffer.clear()
        self._lives = self._backlog = 0
        self.end(CutOff(f"client {self.client!r} fell {self.capacity} events behind"))
        self._release(self)

    def _wake(self) -> None:
        if self._waiter is not None and not self._waiter.done():
            self._waiter.set_result(None)

    def end(self, failure: Exception | None = None) -> None:
        """No more events will arrive; what is buffered is still delivered, then `anext`
        raises `failure` as a `ConnectionError` (or ends, if there is none). A stream that has
        ended stays as it ended: a driver that failed and is then stopped does not turn the
        failure into a clean end."""
        if self._ended:
            return
        self._ended = True
        self._failure = failure
        self._wake()

    def ended(self) -> ConnectionError | None:
        """Why a call by this stream's client is refused, if its stream has ended: the cut-off,
        else "closed" (a driver that stopped or failed is told apart by the caller)."""
        if not self._ended:
            return None
        failure = self._failure
        return (
            failure
            if isinstance(failure, ConnectionError)
            else ConnectionError("the client is closed")
        )

    def take(self) -> list[Event]:
        """Every event held now, removed, without waiting: a server writes them as a batch. The
        bounds apply to what is held, so a consumer that is slow to send a batch lets the next
        one fill again."""
        taken, self._buffer = self._buffer, []
        self._lives = self._backlog = 0
        return taken

    async def close(self) -> None:
        """Disconnect and drop what is buffered."""
        self._buffer.clear()
        self._failure = None
        self.end()
        self._release(self)

    def __aiter__(self) -> "Feed":
        return self

    async def __anext__(self) -> Event:
        while True:
            if self._buffer:
                event = self._buffer.pop(0)
                if isinstance(event, Live):
                    self._lives -= 1
                elif isinstance(event, Appended | Notice):
                    self._backlog -= 1
                return event
            if self._ended:
                failure = self._failure
                if failure is None:
                    raise StopAsyncIteration
                if isinstance(failure, ConnectionError):
                    raise failure
                raise ConnectionError(f"{type(failure).__name__}: {failure}") from failure
            if self._waiter is not None:
                raise RuntimeError("a client has one consumer; another is waiting on it")
            self._waiter = asyncio.get_running_loop().create_future()
            try:
                await self._waiter
            finally:
                self._waiter = None

    async def __aenter__(self) -> "Feed":
        return self

    async def __aexit__(
        self,
        kind: type[BaseException] | None,
        error: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.close()


def _is_state(event: Event) -> bool:
    return isinstance(event, CursorView | CursorGone | Labels)


def _replaces(new: Event, held: Event) -> bool:
    """Whether the state event `new` makes the buffered `held` stale."""
    if isinstance(new, CursorView | CursorGone):
        return isinstance(held, CursorView | CursorGone) and held.cursor == new.cursor
    return type(held) is type(new)


class Hub:
    """The feeds open now, and what was last published to them.

    `questions` says a cursor's open questions, and `lost(client)` is told, once, that a client
    that had a stream open has none now (closed, cut off, or abandoned and collected). `model`
    says the model a node's branch is on, or `None`. A step's ending, a hook's failure and the
    text an extension shows are told to the hub (`began`, `ended`, `hook_failed`, `show`) because
    core keeps none of them."""

    def __init__(
        self,
        session: Session,
        questions: Callable[[int], tuple[OpenQuestion, ...]],
        lost: Callable[[str], None],
        model: Callable[[int], str | None],
    ) -> None:
        self._session = session
        self._questions = questions
        self._lost = lost
        self._model = model
        self._shown: dict[int, dict[str, str]] = {}  # cursor -> extension -> its text
        self._feeds: weakref.WeakSet[Feed] = weakref.WeakSet()
        self._clients: set[str] = set()  # who had a stream open at the last sync
        self._syncing = False
        self._ended: tuple[Exception | None] | None = None
        self._steps: dict[int, tuple[Ending | None, HookFailure | None]] = {}
        self._nodes = len(session.tree)
        self._labels = tuple(session.labels())
        self._views = {c: self._view(c) for c in session.cursors()}
        self._gone = {c: CursorGone(c, at) for c, at in session.closed()}

    def connected(self, client: str) -> bool:
        """Whether `client` has a stream open."""
        return any(client == feed.client for feed in self._feeds)

    def _release(self, feed: Feed) -> None:
        """A stream closed or was cut off: its client may be gone, so publish what that changes."""
        self._feeds.discard(feed)
        self.sync()

    def _streaming(self) -> set[str]:
        return {feed.client for feed in self._feeds}

    def connect(self, client: str, capacity: int) -> Feed:
        """A new stream for `client`, opening with the snapshot: a `CursorView` per open cursor and
        a `CursorGone` per closed one, then `Labels` (which name cursors the client has by then
        been sent, closed ones included), as they stand. Publishing
        what is pending first and taking the snapshot from what was published make it one act:
        nothing falls between the snapshot and what follows. The nodes are not in it: a node
        never changes, so a client reads the ones it needs by id, and ignores an `Appended` one
        it has already read."""
        feed = Feed(client, capacity, self._release)
        if self._ended is not None:  # too late for anything: it ends as the others did
            feed.end(self._ended[0])
            return feed
        self.sync()
        for event in (*self._views.values(), *self._gone.values(), Labels(self._labels)):
            feed.offer(event)
        self._feeds.add(feed)
        self._clients.add(client)
        return feed

    def watch(
        self,
        feed: Feed,
        cursors: Collection[int],
        catch_up: Callable[[int], Sequence[LiveDelta]],
    ) -> None:
        """`feed` hears the `Live` of exactly `cursors` from now on, starting for each cursor
        it did not watch with `catch_up(cursor)`. `NoSuchCursor` for a cursor that is not open."""
        for cursor in cursors:
            self._session.cursor(cursor)
        new = sorted(set(cursors) - feed.watching)
        feed.watching = frozenset(cursors)
        for cursor in new:
            for delta in catch_up(cursor):
                feed.offer(Live(cursor, delta))

    def live(self, cursor: int, delta: LiveDelta) -> None:
        """A provider's delta or a tool's progress, for the clients watching `cursor`."""
        self._send(Live(cursor, delta))

    def began(self, cursor: int) -> None:
        """A step of `cursor` begins: its last ending is stale."""
        self._steps[cursor] = (None, None)

    def ended(self, cursor: int, ending: Ending) -> None:
        """The step of `cursor` ends with `ending`."""
        self._steps[cursor] = (ending, self._steps.get(cursor, (None, None))[1])

    def ending(self, cursor: int) -> Ending | None:
        """How the last step of `cursor` ended; `None` while one runs, and if it has not stepped
        since the session was loaded."""
        return self._steps.get(cursor, (None, None))[0]

    def hook_failed(self, cursor: int, point: str, extension: str, error: str) -> None:
        """A cursor hook failed in the step of `cursor`."""
        ending = self._steps.get(cursor, (None, None))[0]
        self._steps[cursor] = (ending, HookFailure(extension, point, error))

    def show(self, cursor: int, extension: str, text: str | None) -> None:
        """`extension` shows `text` on `cursor`, replacing what it showed; `None` shows nothing.
        Not sent until the next `sync`. `NoSuchCursor` if `cursor` is not open."""
        self._session.cursor(cursor)
        if text is None:
            self._shown.get(cursor, {}).pop(extension, None)
        else:
            self._shown.setdefault(cursor, {})[extension] = text

    def keep_shown(self, extensions: Collection[str]) -> None:
        """Forget the text every extension but `extensions` shows: those are gone. Not sent until
        the next `sync`."""
        for shown in self._shown.values():
            for extension in set(shown) - set(extensions):
                del shown[extension]

    def sync(self) -> None:
        """Send what changed since the last call: the new nodes, then each changed `CursorView`
        (so a view never points at nodes the client has not been sent), then a `CursorGone` for
        each cursor closed since, then `Labels` (so a label never names a cursor the client has
        not been sent), then the session's notices to
        the clients they name. A client that has no stream left is `lost` first, so the views
        sent are the ones its loss changed."""
        if self._ended is not None or self._syncing:
            return
        self._syncing = True
        try:
            while True:  # a stream cut off while sending is a loss to publish too
                for client in sorted(self._clients - self._streaming()):
                    self._lost(client)
                self._clients = self._streaming()
                self._publish()
                if self._clients == self._streaming():
                    return
        finally:
            self._syncing = False

    def _publish(self) -> None:
        session = self._session
        count = len(session.tree)
        if count > self._nodes:
            nodes = tuple(session.tree.node(i) for i in range(self._nodes, count))
            self._nodes = count
            self._send(Appended(nodes))
        for cursor in session.cursors():
            view = self._view(cursor)
            if self._views.get(cursor) != view:
                self._views[cursor] = view
                self._send(view)
        for cursor, at in session.closed():
            if cursor not in self._gone:  # the driver's memory of the cursor goes with its view
                self._gone[cursor] = gone = CursorGone(cursor, at)
                self._views.pop(cursor, None)  # a cursor closed before any sync has none
                self._shown.pop(cursor, None)
                self._steps.pop(cursor, None)
                self._send(gone)
        labels = tuple(session.labels())
        if labels != self._labels:
            self._labels = labels
            self._send(Labels(labels))
        for client, text in session.take_notices():
            self.notify(client, text)

    def notify(self, client: str | None, text: str) -> None:
        """Send `text` to `client` as a `Notice`, or to every client if `None`; dropped if
        `client` is not connected."""
        for feed in tuple(self._feeds):
            if client in (None, feed.client):
                feed.offer(Notice(text))

    def end(self, failure: Exception | None = None) -> None:
        """End every stream (with `failure`, if any) and forget them. Streams connected later
        end the same way; a stop after a failure does not turn it into a clean end."""
        if self._ended is None:
            self._ended = (failure,)
        for feed in tuple(self._feeds):
            feed.end(self._ended[0])
        self._feeds.clear()

    def _send(self, event: Event) -> None:
        for feed in tuple(self._feeds):
            feed.offer(event)

    def _view(self, cursor: int) -> CursorView:
        state = self._session.cursor(cursor)
        ending, hook_error = self._steps.get(cursor, (None, None))
        return CursorView(
            cursor,
            state.at,
            state.busy,
            state.auto,
            self._questions(cursor),
            tuple(state.items),
            ending,
            hook_error,
            self._model(state.at),
            tuple(sorted(self._shown.get(cursor, {}).items())),
        )

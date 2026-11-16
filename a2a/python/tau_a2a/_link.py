"""The connection to the served τ session: one `RemoteClient`, one reader, and the events of each
cursor handed to whoever follows that cursor.

D21 sends every event before the response of the call that caused it, but this reader runs on
its own, so a follower can still hold views from before its call. A follower therefore takes its
feed before the call and decides by what a view says (busy, the pending items, the open
questions), never by which view came first.
"""

import asyncio
import contextlib
from collections.abc import AsyncIterator

from tau import Body, CursorGone, CursorView, Live, RemoteClient

Event = CursorView | Live | CursorGone


class Link:
    """One client of the session. `follow(cursor)` gives that cursor's events from now on."""

    def __init__(self, client: RemoteClient) -> None:
        self.client = client
        self.views: dict[int, CursorView] = {}
        self.followers: dict[int, set[asyncio.Queue[Event | None]]] = {}
        self._watched: set[int] = set()
        self._first_view = asyncio.Event()
        self._reader = asyncio.create_task(self._read())

    @classmethod
    async def connect(cls, socket: str, client_id: str) -> "Link":
        link = cls(await RemoteClient.connect(socket, client_id))
        await asyncio.wait_for(link._first_view.wait(), 30)
        return link

    @property
    def id(self) -> str:
        return self.client.id

    async def _read(self) -> None:
        try:
            async for event in self.client:
                if isinstance(event, CursorView):
                    self.views[event.cursor] = event
                    self._first_view.set()
                    self._hand(event.cursor, event)
                elif isinstance(event, (Live, CursorGone)):
                    self._hand(event.cursor, event)
        finally:
            for queues in self.followers.values():
                for q in queues:
                    q.put_nowait(None)  # the stream ended: followers stop

    def _hand(self, cursor: int, event: Event) -> None:
        for q in self.followers.get(cursor, ()):
            q.put_nowait(event)

    async def follow(self, cursor: int) -> "Feed":
        """Start hearing the cursor: its last view, then every event from now on. Take the feed
        before the call whose effects it is to see, and close it after."""
        q: asyncio.Queue[Event | None] = asyncio.Queue()
        self.followers.setdefault(cursor, set()).add(q)
        if (view := self.views.get(cursor)) is not None:
            q.put_nowait(view)
        if cursor not in self._watched:
            self._watched.add(cursor)
            await self.client.watch(sorted(self._watched))
        return Feed(self, cursor, q)

    async def prefix_end(self) -> int:
        """The node a new context starts from by default: on the lowest open cursor's walk, the
        node before its first `User` node (the session's prefix: its config and system text), or
        the cursor's node if nothing was said yet."""
        cursor = min(self.views)
        walk = await self.client.walk(self.views[cursor].at)
        last = walk[0].id
        for node in walk:
            if isinstance(node.body, Body.User):
                break
            last = node.id
        return last

    async def close(self) -> None:
        self._reader.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await self._reader
        await self.client.close()


class Feed:
    """One follower's events of one cursor, in order. Iterating ends with `ConnectionError` if
    the session's stream ends."""

    def __init__(self, link: Link, cursor: int, q: "asyncio.Queue[Event | None]") -> None:
        self._link, self._cursor, self._q = link, cursor, q

    def __aiter__(self) -> AsyncIterator[Event]:
        return self._events()

    async def _events(self) -> AsyncIterator[Event]:
        while (event := await self._q.get()) is not None:
            yield event
        raise ConnectionError("the session's stream ended")

    def close(self) -> None:
        self._link.followers.get(self._cursor, set()).discard(self._q)

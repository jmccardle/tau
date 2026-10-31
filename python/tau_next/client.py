"""A frontend's whole view of a session (D16 section 5): calls, reads, and one event stream.

A frontend never touches `Driver` or `Session`. It holds a `Client`: async calls that act for
the client's id (`by: Frontend(client)`), async reads of the tree, and an event stream, which
is the client itself (`async for event in client`). `LocalClient` is the in-process
implementation; a remote one serializes each call and event, which are all plain frozen values,
so the protocol has no call that takes or returns a handle.
"""

from collections.abc import AsyncIterator, Collection, Mapping
from typing import Protocol

from tau_next._commands import CommandInfo
from tau_next._core import Node
from tau_next._hub import Feed
from tau_next._options import OptionsForm
from tau_next.driver import Driver
from tau_next.events import Event


class Client(AsyncIterator[Event], Protocol):
    """What a frontend holds. Iterating it yields the events: `Connected`, `Labels` and a
    `CursorView` per cursor first, then changes as they happen, until the driver stops (the
    stream ends), fails (it raises the failure), or the client is `CutOff` for falling behind.
    Every call raises once the driver has failed or stopped."""

    id: str
    """The client's id: stable across reconnects (`john@laptop:tui`)."""

    async def input(self, cursor: int, text: str) -> int:
        """Add `text` to the cursor's scratchpad as this client's input; returns the item."""
        ...

    async def send(self, cursor: int, text: str) -> int:
        """`input` with auto-step on."""
        ...

    async def step(self, cursor: int) -> None:
        """Start a step on a cursor at rest; `Busy` otherwise."""
        ...

    async def go_to(self, cursor: int, to: int) -> bool:
        """Move the cursor to node `to`, aborting a busy step (which declines its open
        questions). True if a running step was cancelled."""
        ...

    async def set_auto(self, cursor: int, on: bool) -> None: ...

    async def abort(self, cursor: int) -> bool:
        """Abort the cursor's step and decline its open questions. True if a running step was
        cancelled."""
        ...

    async def open(self, at: int) -> int:
        """Open a cursor at node `at`; it wakes. Returns its id."""
        ...

    async def set_label(self, label: str, cursor: int) -> None:
        """Put `label` on `cursor`; a label set elsewhere moves."""
        ...

    async def remove_label(self, label: str) -> None:
        """Remove `label`; `NoSuchLabel` if there is none."""
        ...

    async def watch(self, cursors: Collection[int]) -> None:
        """Hear the `Live` of exactly these cursors from now on (a call replaces the last),
        starting with what each sample in flight has streamed that is not in the tree yet."""
        ...

    async def answer(self, question: int, value: object) -> None:
        """Answer an open question with a decoded JSON value; `None` declines. `ValueError` if
        the value is wrong (the question stays open), `LookupError` if the question is closed
        or unknown (the first good answer wins), `PermissionError` if it is for another
        client."""
        ...

    async def commands(self) -> tuple[CommandInfo, ...]:
        """Each command's name, description and argument schema; fixed for the driver's life."""
        ...

    async def run(self, cursor: int, command: str, args: Mapping[str, object]) -> None:
        """Run `/command` with decoded JSON `args` on `cursor`. The result and any failure
        come back as a `Notice` to this client. `LookupError` for an unknown command,
        `ValueError` naming the problem for arguments that fit no parameter (the values are
        checked with the session, `"new"` last), `Busy` for a command that runs as a step of
        the cursor when it is not at rest."""
        ...

    async def options(self, cursor: int, extension: str) -> OptionsForm:
        """`extension`'s options as they stand at `cursor`, with their schema: the form
        `/config` takes the changed values of. `OptionsError` if it has no options."""
        ...

    async def complete(self, cursor: int, named: str, prefix: str) -> list[str]:
        """The values of a named type (`"path"`, `"model"`, `"cursor"`, `"label"`, `"node"`;
        the `"format"` of a schema) that start with `prefix`, as `cursor` sees them. Paths are
        the driver's machine's, relative to its working directory."""
        ...

    async def node(self, id: int) -> Node: ...

    async def walk(self, id: int) -> list[Node]:
        """The nodes from the root to `id`."""
        ...

    async def children(self, id: int) -> list[int]: ...

    async def close(self) -> None:
        """Disconnect: the stream ends and the client is no longer connected."""
        ...


class LocalClient:
    """A `Client` in the driver's process. Creating one connects: its stream opens with the
    snapshot, in one act on the loop, so build it inside the running loop. `buffer` bounds the
    stream (`Feed`). The client is connected while its stream is open: `close()` (or dropping
    the client) disconnects it, and a reconnect is a new `LocalClient` with the same id."""

    def __init__(self, driver: Driver, id: str, *, buffer: int = 256) -> None:
        self.id = id
        self._driver = driver
        self._feed: Feed = driver.connect(id, buffer)

    def __aiter__(self) -> "LocalClient":
        return self

    async def __anext__(self) -> Event:
        return await anext(self._feed)

    async def close(self) -> None:
        await self._feed.close()

    async def input(self, cursor: int, text: str) -> int:
        return self._driver.input(cursor, self.id, text)

    async def send(self, cursor: int, text: str) -> int:
        return self._driver.send(cursor, self.id, text)

    async def step(self, cursor: int) -> None:
        self._driver.step(cursor)

    async def go_to(self, cursor: int, to: int) -> bool:
        return self._driver.go_to(cursor, to, self.id)

    async def set_auto(self, cursor: int, on: bool) -> None:
        self._driver.set_auto(cursor, on)

    async def abort(self, cursor: int) -> bool:
        return self._driver.abort(cursor)

    async def open(self, at: int) -> int:
        return self._driver.open(at, self.id)

    async def set_label(self, label: str, cursor: int) -> None:
        self._driver.set_label(label, cursor)

    async def remove_label(self, label: str) -> None:
        self._driver.remove_label(label)

    async def watch(self, cursors: Collection[int]) -> None:
        self._driver.watch(self._feed, cursors)

    async def answer(self, question: int, value: object) -> None:
        self._driver.answer(self.id, question, value)

    async def commands(self) -> tuple[CommandInfo, ...]:
        return self._driver.commands.infos()

    async def run(self, cursor: int, command: str, args: Mapping[str, object]) -> None:
        self._driver.commands.run(self.id, cursor, command, args)

    async def options(self, cursor: int, extension: str) -> OptionsForm:
        return self._driver.commands.form(cursor, extension)

    async def complete(self, cursor: int, named: str, prefix: str) -> list[str]:
        return await self._driver.commands.complete(cursor, named, prefix)

    async def node(self, id: int) -> Node:
        return self._driver.session.tree.node(id)

    async def walk(self, id: int) -> list[Node]:
        return self._driver.session.tree.walk(id)

    async def children(self, id: int) -> list[int]:
        return self._driver.session.tree.children(id)

    async def __aenter__(self) -> "LocalClient":
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

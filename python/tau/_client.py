"""A frontend's whole view of a session (D16 section 5): calls, reads, and one event stream.

A frontend never touches `Driver` or `Session`. It holds a `Client`: async calls that act for
the client's id (`by: Frontend(client)`), async reads of the tree, and an event stream, which
is the client itself (`async for event in client`). `LocalClient` is the in-process
implementation, made by `Driver.client`; a remote one serializes each call and event, so the
protocol has no call that takes or returns a handle.

A stream that ends in error raises `ConnectionError`: `CutOff` for a client that fell behind,
or one whose message is the `"Type: msg"` of the failure that ended the driver. So does a call
on a driver that has failed (the same message, the failure chained) or stopped ("the driver is
stopped"), and a call on a client that was closed or cut off: a client is its connection. A
frontend has one rule for all of them: `except ConnectionError`, then connect again.
"""

import asyncio
from collections.abc import AsyncIterator, Collection, Mapping
from typing import Protocol, Self, cast

from tau._calls import Calls
from tau._core import Node
from tau._events import Event
from tau._hub import Feed
from tau._values import Form
from tau.commandline import Parsed, completions, parse


class _TreeReads(Protocol):
    """The reads of the tree, a part of `Client`: nodes never change and ids are dense."""

    async def node(self, id: int) -> Node:
        """The node with this id; `NoSuchNode` if there is none."""
        ...

    async def nodes(self, start: int, limit: int) -> list[Node]:
        """Up to `limit` nodes from id `start`, in id order; none past the end. Ids are dense
        and a node never changes, so a tree is read in ranges. `ValueError` for a negative
        `start` or `limit`."""
        ...

    async def walk(self, id: int) -> list[Node]:
        """The nodes from the root to `id`."""
        ...

    async def children(self, id: int) -> list[int]:
        """The ids of the children of node `id`, in append order."""
        ...


class _CommandCalls(Protocol):
    """The command line's calls, a part of `Client`: commands, options, completion."""

    async def commands(self) -> tuple[Form, ...]:
        """Each command's form: its name, description and argument schema (named types with
        their `"format"`, each default), fixed until the driver reloads. If some extension has
        options there is one more, `config`, whose one argument is the extension; it is
        changed with `configure`, not `run`."""
        ...

    async def run(self, cursor: int, command: str, args: Mapping[str, object]) -> str | None:
        """Run `/command` (`"note"`, or `"a:note"`, the extension's qualified name) with decoded
        JSON `args` on `cursor`; returns the command's result
        (`None` if it has none) once it has finished. `CommandFailed` (its message is the
        failure as `"Type: msg"`) if it raised, or was aborted; it is raised as soon as the
        command raises, even when the command's step has not yet ended: the cursor may still be
        busy for a moment while the step ends, and a retry then gets `Busy`. A command's `progress`
        and `cursor.tell` arrive as `Notice`s. `LookupError` for an unknown command or a bare name
        two extensions share (`"/note is two commands: /a:note, /b:note"`), `ValueError`
        naming the problem for arguments that fit no parameter (the values are checked with the
        session, `"new"` last), `Busy` for a command that runs as a step of the cursor when it is
        not at rest."""
        ...

    async def options(self, cursor: int, extension: str) -> Form:
        """`extension`'s options as they stand at `cursor`: a form whose schema has every
        option, each default the value now. `ConfigError` if it has no options."""
        ...

    async def configure(
        self, cursor: int, extension: str, values: Mapping[str, object], *, save: bool = False
    ) -> None:
        """Change the options named in `values` (decoded JSON) of `extension` on `cursor`'s
        branch: a step of the cursor writes a `Config` node. With `save`, also write them into
        the user's config file (`extensions.<name>`), which every session reads. `ConfigError`
        for an extension without options, or values its options type refuses (or, with `save`,
        a driver with no user config), `Busy` unless the cursor is at rest; nothing is written
        then, neither node nor file."""
        ...

    async def complete(self, cursor: int, named: str, prefix: str) -> list[str]:
        """The values of a named type (`"path"`, `"model"`, `"cursor"`, `"label"`, `"node"`;
        the `"format"` of a schema) that start with `prefix`, as `cursor` sees them. Paths are
        the driver's machine's, relative to its working directory."""
        ...

    async def parse_line(self, cursor: int, line: str) -> Parsed:
        """`commandline.parse` of `line` with this driver's forms and the options forms
        `cursor` sees: the command, its arguments, and the required parameters still `missing`.
        `UnknownCommand` if no command has the name (send the line as text), `ValueError` for a
        line that cannot be read, plain `LookupError` for a bare name two extensions share."""
        ...

    async def complete_line(self, cursor: int, line: str) -> list[str]:
        """`commandline.completions` of `line` on the driver: what could finish its last
        word, in one call."""
        ...


class Client(_TreeReads, _CommandCalls, AsyncIterator[Event], Protocol):
    """What a frontend holds. Iterating it yields the events: first a snapshot (a `CursorView` per
    open cursor, a `CursorGone` per closed one, then `Labels`), then changes as they happen,
    until the driver stops (the stream ends), or the driver fails or the client is `CutOff` for
    falling behind (it raises `ConnectionError`). Every call raises `ConnectionError` once the
    driver has failed or stopped, or the client has been closed or cut off."""

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

    async def go_to(self, cursor: int, to: int) -> None:
        """Move the cursor to node `to`, aborting a busy step (which declines its open
        questions)."""
        ...

    async def remove_item(self, cursor: int, item: int) -> None:
        """Take back a pending input this client added to `cursor` (`input` or `send`): it is
        removed from the scratchpad before the cursor delivers it. `LookupError` once it is
        delivered, or if there is no such item on the cursor; `PermissionError` for an item
        another client or an extension added. Wakes nothing and does not turn auto-step off."""
        ...

    async def set_auto(self, cursor: int, on: bool) -> None:
        """Turn `cursor`'s auto-step on or off; on wakes it."""
        ...

    async def abort(self, cursor: int) -> None:
        """Abort the cursor's step and decline its open questions."""
        ...

    async def open(self, at: int) -> int:
        """Open a cursor at node `at`; it wakes. Returns its id."""
        ...

    async def close_cursor(self, cursor: int) -> None:
        """Close the cursor (D17): abort its step as `abort` does, wait for it to rest, drop its
        pending items, and close it. Clients are sent a `CursorGone`. Its id, last node and
        labels stay; `reopen` restarts from them. `CursorClosed` if it is already closed."""
        ...

    async def reopen(self, cursor: int) -> int:
        """Open a new cursor at the closed `cursor`'s last node, and move each of its labels
        onto it. Returns the new id. `ValueError` if `cursor` is open, `NoSuchCursor` if it was
        never opened."""
        ...

    async def set_label(self, cursor: int, label: str) -> None:
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

    async def close(self) -> None:
        """Disconnect: the stream ends and the client is no longer connected."""
        ...


class _Stream:
    """A client's id and its event stream: connected while the stream is open, so `close()` (or
    dropping the client) disconnects it, and a reconnect is a new client with the same id."""

    def __init__(self, calls: Calls, id: str, buffer: int) -> None:
        self.id = id
        self._calls = calls
        self._feed: Feed = calls.connect(id, buffer)

    def __aiter__(self) -> Self:
        return self

    async def __anext__(self) -> Event:
        return await anext(self._feed)

    def take(self) -> list[Event]:
        """The events the stream holds now, removed, without waiting. A server writes a
        client's events as batches and sends each response after the events its call caused."""
        return self._feed.take()

    async def close(self) -> None:
        """Disconnect: the stream ends and the client is no longer connected."""
        await self._feed.close()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()


async def _finished(calls: Calls, outcome: "asyncio.Future[str | None]") -> str | None:
    """What a command came to. A command the driver gave up on, because it failed or was
    stopped, is cancelled: the call says so."""
    try:
        return await outcome
    except asyncio.CancelledError:
        calls.check()
        raise


class _Connected(_Stream):
    """A client of the driver in this process, once connected."""

    def _up(self) -> Calls:
        """The calls, if the driver is running and this client is connected: every call starts
        here, so each one raises `ConnectionError` the same way once the driver has failed or
        stopped, or the client was closed or cut off."""
        self._calls.check()
        gone = self._feed.ended()
        if gone is not None:
            raise gone
        return self._calls


class _LocalTreeReads(_Connected):
    """`LocalClient`'s reads of the tree."""

    async def node(self, id: int) -> Node:
        return self._up().session.tree.node(id)

    async def nodes(self, start: int, limit: int) -> list[Node]:
        return self._up().nodes(start, limit)

    async def walk(self, id: int) -> list[Node]:
        return self._up().session.tree.walk(id)

    async def children(self, id: int) -> list[int]:
        return self._up().session.tree.children(id)


class _LocalCommandCalls(_Connected):
    """`LocalClient`'s command-line calls."""

    async def commands(self) -> tuple[Form, ...]:
        return self._up().commands.forms()

    async def run(self, cursor: int, command: str, args: Mapping[str, object]) -> str | None:
        calls = self._up()
        return await _finished(calls, calls.commands.run(self.id, cursor, command, args))

    async def options(self, cursor: int, extension: str) -> Form:
        return self._up().commands.options(cursor, extension)

    async def configure(
        self, cursor: int, extension: str, values: Mapping[str, object], *, save: bool = False
    ) -> None:
        calls = self._up()
        await _finished(calls, calls.commands.configure(self.id, cursor, extension, values, save))

    async def complete(self, cursor: int, named: str, prefix: str) -> list[str]:
        return await self._up().commands.complete(cursor, named, prefix)

    async def parse_line(self, cursor: int, line: str) -> Parsed:
        commands = self._up().commands
        return parse(line, commands.forms(), commands.option_forms(cursor))

    async def complete_line(self, cursor: int, line: str) -> list[str]:
        commands = self._up().commands
        return await completions(
            line, commands.forms(), cast("Client", self), cursor, commands.option_forms(cursor)
        )


class LocalClient(_LocalCommandCalls, _LocalTreeReads, Client):
    """A `Client` in the driver's process, made by `Driver.client`: its stream opens with the
    snapshot, in one act on the loop, so make it inside the running loop. Each call is
    documented on `Client`."""

    async def input(self, cursor: int, text: str) -> int:
        return self._up().input(cursor, self.id, text)

    async def send(self, cursor: int, text: str) -> int:
        return self._up().send(cursor, self.id, text)

    async def step(self, cursor: int) -> None:
        self._up().step(cursor)

    async def go_to(self, cursor: int, to: int) -> None:
        self._up().go_to(cursor, to, self.id)

    async def remove_item(self, cursor: int, item: int) -> None:
        self._up().remove_item(cursor, self.id, item)

    async def set_auto(self, cursor: int, on: bool) -> None:
        self._up().set_auto(cursor, on)

    async def abort(self, cursor: int) -> None:
        self._up().abort(cursor)

    async def open(self, at: int) -> int:
        return self._up().open(at, self.id)

    async def close_cursor(self, cursor: int) -> None:
        await self._up().close_cursor(cursor)

    async def reopen(self, cursor: int) -> int:
        return self._up().reopen(cursor, self.id)

    async def set_label(self, cursor: int, label: str) -> None:
        self._up().set_label(cursor, label)

    async def remove_label(self, label: str) -> None:
        self._up().remove_label(label)

    async def watch(self, cursors: Collection[int]) -> None:
        self._up().watch(self._feed, cursors)

    async def answer(self, question: int, value: object) -> None:
        self._up().answer(self.id, question, value)

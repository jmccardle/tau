"""What a hook is handed: a cursor or node handle, and the thing it works on."""

from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from functools import partial
from typing import Any, Protocol

from tau._config import model_config
from tau._core import (
    Author,
    Body,
    Config,
    CursorState,
    Draft,
    Ending,
    HandleWrites,
    ItemData,
    Node,
    NoSuchCursor,
    NotInHook,
    Session,
    Tree,
)
from tau._values import DefinitionError, Kind, Model, kind_of

Poke = Callable[[], None]
"""Tells the driver's pump a handle call may have woken a cursor."""

Changed = Callable[[], None]
"""Tells the driver a handle call changed the session: it publishes the difference."""

Now = Callable[[], int]
"""The driver's clock, in milliseconds."""

Notify = Callable[[str | None, str], None]
"""Sends a `Notice` to a client, or to every client when `None`; dropped if that client is not
connected."""

Rest = Callable[[int], Awaitable[Ending | None]]
"""The driver's wait for a cursor to come to rest: returns how its last step ended, or `None`
if that is not known."""

Abort = Callable[[int], None]
"""The driver's abort of a cursor's step. Safe to call from any thread: it returns once the
loop has done it."""

Close = Callable[[int], Awaitable[None]]
"""The driver's close of a cursor: abort, wait for rest, close. Returns once it is closed."""

Asker = Callable[[str, Kind, str | None, Callable[[], int]], Awaitable[object]]
"""The driver's question call for one cursor: `(text, expect, to, new_cursor) -> the checked and
converted answer, or None`. `new_cursor` opens the cursor a `CursorId` answer of `"new"` means."""


class OptionsReader(Protocol):
    """What a handle reads and checks options by: the driver's `ExtensionOptions`."""

    def at(self, extension: str, node: int) -> Any:
        """The extension's options instance at `node`."""
        ...

    def check_config(
        self, at: int, model: str | None, extensions: Mapping[str, Mapping[str, object]]
    ) -> None:
        """Raise `ConfigError` unless a `Config` node under `at` setting `model` and `extensions`
        is valid: a model in the catalog, options that fit their extension's type once folded."""
        ...


def reopened(
    session: Session,
    cursor: int,
    open: Callable[[int], int],
    set_label: Callable[[str, int], None],
) -> int:
    """`reopen`, for whoever opens and labels as themselves: open a cursor at the closed
    `cursor`'s last node, and move each of its labels onto it. `NoSuchCursor` if `cursor` is
    not closed (open, or never opened)."""
    closed = dict(session.closed())
    if cursor not in closed:
        raise NoSuchCursor(f"cursor {cursor} is not closed")
    new = open(closed[cursor])
    for label, on in session.labels():
        if on == cursor:
            set_label(label, new)
    return new


async def _ask(
    asker: Asker,
    text: str,
    expect: object,
    to: str | None,
    new_cursor: Callable[[], int],
) -> Any:
    """What `ask` does on either handle: read `expect` as a hint of the type language
    (`DefinitionError` if it is not one) and wait for the answer."""
    kind = kind_of(expect)
    if kind is None:
        raise DefinitionError(f"cannot ask for {expect!r}: not a type the driver can check")
    return await asker(text, kind, to, new_cursor)


@dataclass
class Subject:
    """The mutable thing a cursor hook works on, by hook point.

    `draft` for `pre_input`, `pre_sample`, `pre_reply` (a hook may replace it;
    from `pre_sample` it is a reply that answers the sample). `node` for
    `post_reply` and `post_result`. `ending` for `post_step`. `pre_step` has
    none.
    """

    draft: Draft | None = None
    node: Node | None = None
    ending: Ending | None = None


class TreeHandle:
    """What both handles share: reads of the whole session, and the writes that
    belong to no cursor, bound to an extension (which fills in `by`).

    A handle has no client: work on a cursor does not depend on who is watching it.

    Every call goes into Rust at once. Nothing here moves a cursor.
    """

    def __init__(  # noqa: PLR0913, PLR0917 - a handle's whole wiring
        self,
        session: Session,
        extension: str,
        now: Now,
        poke: Poke,
        changed: Changed,
        rest: Rest,
        abort: Abort,
        close: Close,
        own: int | None = None,
    ) -> None:
        self._session = session
        self._now = now
        self._poke = poke
        self._changed = changed
        self._rest = rest
        self._abort = abort
        self._close = close
        self._own = own  # the cursor this handle works for, if it has one
        # A tree handle's writes sync at once. Only `Cursor` rebinds them to a hook: nothing
        # public takes a hook id, and `HandleWrites` is in the private `_core`.
        self._writes = HandleWrites(session, None)
        self._live = True
        self.extension = extension

    @property
    def by(self) -> Author:
        return Author.Extension(self.extension)

    def retire(self) -> None:
        """Called by the driver when the hook coroutine returns: the handle is dead.
        Its reads still work; everything else raises `NotInHook`."""
        self._live = False

    def _check(self) -> None:
        if not self._live:
            raise NotInHook(f"the hook of extension {self.extension!r} has returned")

    # Reads

    @property
    def tree(self) -> Tree:
        """The session's nodes."""
        return self._session.tree

    def cursor(self, cursor: int) -> CursorState:
        """`cursor` as it stands now: `at`, `auto`, `items`, `labels`, `moves`, ..."""
        return self._session.cursor(cursor)

    def cursors(self) -> list[int]:
        return self._session.cursors()

    def closed(self) -> list[tuple[int, int]]:
        """Each closed cursor and its last node, in id order."""
        return self._session.closed()

    def label(self, label: str) -> int:
        """The cursor `label` is on (a closed cursor too); raises `NoSuchLabel`."""
        return self._session.label(label)

    # Writes

    def append(self, parent: int, body: Body) -> int:
        """Append a node under `parent` without moving any cursor. Returns its id."""
        self._check()
        node = self._writes.append(parent, body, self.by, self._now())
        self._changed()
        return node

    def append_root(self, config: Config) -> int:
        """Append a root (a detached tree) holding `config`, without moving any
        cursor. Returns its id."""
        self._check()
        node = self._writes.append_root(config, self.by, self._now())
        self._changed()
        return node

    def open(self, at: int) -> int:
        """Open a cursor at `at`; it wakes. Returns its id."""
        self._check()
        cursor = self._writes.open(at, self.by, self._now())
        self._changed()
        self._poke()
        return cursor

    def add_item(self, to: int, data: str) -> int:
        """Add this extension's item to a cursor's scratchpad; the cursor wakes."""
        self._check()
        item = self._writes.add_item(to, ItemData.Extension(self.extension, data))
        self._changed()
        self._poke()
        return item

    def remove_item(self, id: int) -> None:
        """Remove any item; raises `NoSuchItem`. Wakes nothing."""
        self._check()
        self._writes.remove_item(id)
        self._changed()

    def send(self, to: int, text: str) -> int:
        """Send `text` to cursor `to` as a `User` message authored by this extension, and turn
        its auto-step on: `to` takes the text at its next step (through its `pre_input` hooks)
        and answers. Needs no client. Returns the item."""
        self._check()
        item = self._session.input(to, self.by, text)
        self._session.set_auto(to, True)
        self._changed()
        self._poke()
        return item

    async def rest(self, cursor: int) -> Ending | None:
        """Wait until `cursor` is at rest with no effect out, and return how its last step
        ended; `None` if that is not known (it has not stepped since the session was loaded).
        After a `send` to a cursor that was at rest, this is the ending of the step the send
        caused. If the cursor was mid-step, the input waits for that step to end and this may
        return that earlier step's ending with the input still pending: to wait for the step a
        send causes, `rest` first, then `send`, then `rest` again. A cursor held by an open
        question or a gate does not rest, so this waits as long as the cursor is held. Async
        only: a sync tool, which runs on a worker thread, cannot call it. `ValueError` for this
        handle's own cursor: waiting for itself would never end. No cycle between two cursors
        is detected; the user's abort is the way out."""
        self._check()
        self._refuse_own(cursor, "rest", "waiting for itself would never end")
        return await self._rest(cursor)

    def abort(self, cursor: int) -> None:
        """Abort `cursor`'s step as a client's `abort` does: cancel its sample or calls, stop
        its command, decline its open questions, and turn its auto-step off. Safe from a sync
        tool's worker thread: it returns once the event loop has done it. `ValueError` for this
        handle's own cursor: aborting it would cancel this very call."""
        self._check()
        self._refuse_own(cursor, "abort", "aborting itself would cancel this very call")
        self._abort(cursor)

    async def close_cursor(self, cursor: int) -> None:
        """Close `cursor`: abort its step as `abort` does, wait for it to rest, drop its pending
        items, and close it (D17). It leaves `cursors()` and the clients' views; its id, its last
        node (in `closed()`) and its labels stay, which is what `reopen` restarts from. Anyone
        waiting on it sees the abort. `CursorClosed` if it is already closed. Async only, like
        `rest`. `ValueError` for this handle's own cursor: closing it would wait on itself."""
        self._check()
        self._refuse_own(cursor, "close_cursor", "closing it would wait on itself")
        await self._close(cursor)

    def reopen(self, cursor: int) -> int:
        """Open a new cursor at the closed `cursor`'s last node and move each of its labels
        onto it, as this extension. Returns the new id. `NoSuchCursor` if `cursor` is not
        closed."""
        self._check()
        return reopened(self._session, cursor, self.open, self.set_label)

    def _refuse_own(self, cursor: int, call: str, why: str) -> None:
        if cursor == self._own:
            raise ValueError(f"{call}: cursor {cursor} is this handle's own; {why}")

    def set_label(self, label: str, cursor: int) -> None:
        """Set `label` on `cursor`; a label set elsewhere moves."""
        self._check()
        self._writes.set_label(label, cursor)
        self._changed()

    def remove_label(self, label: str) -> None:
        """Remove `label`; raises `NoSuchLabel`."""
        self._check()
        self._writes.remove_label(label)
        self._changed()


class NodeHandle(TreeHandle):
    """What a tool hook acts through: the batch's node, bound to its extension.

    The tree handle, plus the batch's node (`id`), the cursor whose batch it is (`cursor_id`),
    and `ask`, which goes to that cursor. Has none of the cursor's own operations.
    """

    def __init__(  # noqa: PLR0913, PLR0917 - a handle's whole wiring
        self,
        session: Session,
        id: int,
        cursor: int,
        extension: str,
        asker: Asker,
        now: Now,
        poke: Poke,
        changed: Changed,
        rest: Rest,
        abort: Abort,
        close: Close,
        options: OptionsReader,
    ) -> None:
        super().__init__(session, extension, now, poke, changed, rest, abort, close, cursor)
        self._asker = asker
        self._options = options
        self.id = id
        self.cursor_id = cursor

    @property
    def node(self) -> Node:
        return self.tree.node(self.id)

    def options(self) -> Any:
        """This extension's options at the batch's node (its `options` dataclass); raises
        `ConfigError` if it has none or what is set does not fit."""
        return self._options.at(self.extension, self.id)

    def walk(self) -> list[Node]:
        return self.tree.walk(self.id)

    async def ask(self, text: str, expect: object = str, *, to: str | None = None) -> Any:
        """Ask the batch's cursor's clients `text`, and wait for an answer of type `expect`
        (any hint of the type language; `str` by default). Returns the checked, converted
        answer, or `None` if declined. `to` names the client asked first (`None`: anyone; a
        client that is not connected is the same). The call stays out meanwhile, however long and
        whoever is connected, so the cursor is busy; an abort or go-to declines it (and any asked
        later in that step). A `CursorId` answer of `"new"` opens a cursor at the batch's node."""
        self._check()
        return await _ask(self._asker, text, expect, to, lambda: self.open(self.id))


class Cursor(TreeHandle):
    """What a cursor hook acts through: one cursor, bound to its extension.

    The tree handle, plus the cursor's own `move`, `write`, and `set_auto`,
    which core allows only while a cursor hook job is out for the cursor. Once
    the hook coroutine returns (`retire`), they raise `NotInHook`; so does every
    other write and `ask`. Reads still work. Core checks no more than that the
    tree stays valid (no hook edit limits).
    """

    def __init__(  # noqa: PLR0913, PLR0917 - a handle's whole wiring
        self,
        session: Session,
        id: int,
        extension: str,
        asker: Asker,
        notify: Notify,
        now: Now,
        poke: Poke,
        changed: Changed,
        rest: Rest,
        abort: Abort,
        close: Close,
        options: OptionsReader,
    ) -> None:
        super().__init__(session, extension, now, poke, changed, rest, abort, close, id)
        self._writes = HandleWrites(session, id)  # the writes ride this hook's phase
        self._asker = asker
        self._notify = notify
        self._options = options
        self.id = id

    def node(self, id: int | None = None) -> Node:
        return self.tree.node(self.cursor(self.id).at if id is None else id)

    def walk(self) -> list[Node]:
        return self.tree.walk(self.cursor(self.id).at)

    def options(self) -> Any:
        """This extension's options at the cursor's `at` (its `options` dataclass); raises
        `ConfigError` if it has none or what is set does not fit."""
        return self._options.at(self.extension, self.cursor(self.id).at)

    def set_model(self, model: Model) -> int:
        """Write a `Config` node under `at` that sets the branch's `model` (a qualified name)
        and move onto it, as `write` does. `ConfigError` if it is not configured; nothing is
        written then. Returns the new node's id."""
        self._options.check_config(self.cursor(self.id).at, model, {})
        return self.write(Body.Config(model_config(model)))

    def set_options(self, **changes: object) -> int:
        """Write a `Config` node under `at` that sets these options of this extension (JSON
        values) and move onto it, as `write` does: each replaces the walk's earlier value.
        `ConfigError` if the extension has no options, or those that result, folded with the
        walk's, do not fit its options type (a `__post_init__` that raises refuses); nothing is
        written then. Returns the new node's id."""
        extensions = {self.extension: changes}
        self._options.check_config(self.cursor(self.id).at, None, extensions)
        return self.write(Body.Config(model_config(extensions=extensions)))

    def move(self, to: int) -> None:
        """Move to `to` at once; later hooks see the new `at`. Raises `NoSuchNode`."""
        self._check()
        self._session.hook_move(self.id, to, self.by, self._now())
        self._changed()

    def write(self, body: Body, delivers: Sequence[int] = ()) -> int:
        """Append `body` under `at` and move onto it, as one record; the
        `delivers` items are removed. Returns the new node's id."""
        self._check()
        node = self._session.hook_write(self.id, body, list(delivers), self.by, self._now())
        self._changed()
        return node

    def set_auto(self, on: bool) -> None:
        """Turn this cursor's auto-step on or off. Wakes nothing: the step is running.
        Turning it on inside a step that is aborted leaves the cursor at rest."""
        self._check()
        self._session.hook_set_auto(self.id, on)
        self._changed()

    def tell(self, text: str, *, to: str | None = None) -> None:
        """Send `text` as a `Notice` to client `to`, or to every connected client by default.
        Dropped if `to` is not connected: a notice is for someone who is there."""
        self._check()
        self._changed()  # what was queued first goes first
        self._notify(to, text)

    async def ask(self, text: str, expect: object = str, *, to: str | None = None) -> Any:
        """Ask the cursor's clients `text`, and wait for an answer of type `expect` (any hint
        of the type language; `str` by default). Returns the checked, converted answer, or
        `None` if declined. `to` names the client asked first (`None`: anyone; a client that is
        not connected is the same). The hook's job stays out meanwhile, however long and whoever
        is connected, so the cursor is busy; an abort or go-to declines it (and any asked later
        in that step). A `CursorId` answer of `"new"` opens a cursor at the cursor's `at`."""
        self._check()
        return await _ask(self._asker, text, expect, to, lambda: self.open(self.cursor(self.id).at))


@dataclass(frozen=True)
class Handles:
    """What the driver wires into every handle it makes: the session, the question call (by
    cursor), the `Notice` call, the clock, `poke` and `changed`, and the options. One place that
    builds them."""

    session: Session
    ask: Callable[..., Awaitable[object]]
    notify: Notify
    now: Now
    poke: Poke
    changed: Changed
    options: OptionsReader
    rest: Rest
    abort: Abort
    close: Close

    def cursor(self, id: int, extension: str) -> Cursor:
        """The handle of cursor `id` for `extension`."""
        return Cursor(
            self.session,
            id,
            extension,
            partial(self.ask, id),
            self.notify,
            self.now,
            self.poke,
            self.changed,
            self.rest,
            self.abort,
            self.close,
            self.options,
        )

    def node(self, cursor: int, id: int, extension: str) -> NodeHandle:
        """The handle of node `id`, the batch of `cursor`, for `extension`."""
        return NodeHandle(
            self.session,
            id,
            cursor,
            extension,
            partial(self.ask, cursor),
            self.now,
            self.poke,
            self.changed,
            self.rest,
            self.abort,
            self.close,
            self.options,
        )

    def tree(self, extension: str) -> TreeHandle:
        """A handle on no cursor or node, for `extension`."""
        return TreeHandle(
            self.session,
            extension,
            self.now,
            self.poke,
            self.changed,
            self.rest,
            self.abort,
            self.close,
        )

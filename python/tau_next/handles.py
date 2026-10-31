"""What a hook is handed: a cursor or node handle, and the thing it works on."""

from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from functools import partial
from typing import Any, Protocol

from tau_next._config import config_node
from tau_next._core import (
    Author,
    Body,
    Config,
    CursorState,
    Draft,
    Ending,
    HandleWrites,
    ItemData,
    Node,
    NotInHook,
    Session,
    Tree,
)
from tau_next.values import HintError, Kind, kind_of

Poke = Callable[[], None]
"""Tells the driver's pump a handle call may have woken a cursor."""

Changed = Callable[[], None]
"""Tells the driver a handle call changed the session: it publishes the difference."""

Now = Callable[[], int]
"""The driver's clock, in milliseconds."""

Asker = Callable[[str, Kind, str | None, Callable[[], int]], Awaitable[object]]
"""The driver's question call for one cursor: `(text, expect, to, new_cursor) -> the checked and
converted answer, or None`. `new_cursor` opens the cursor a `CursorId` answer of `"new"` means."""


class OptionsReader(Protocol):
    """What a handle reads and checks options by: the driver's `ExtensionOptions`."""

    def at(self, extension: str, node: int) -> Any:
        """The extension's options instance at `node`."""
        ...

    def check(self, extensions: Mapping[str, Mapping[str, object]]) -> None:
        """Raise unless `extensions` names options an extension has, fitting its type."""
        ...


async def _ask(
    asker: Asker,
    text: str,
    expect: object,
    to: str | None,
    new_cursor: Callable[[], int],
) -> Any:
    """What `ask` does on either handle: read `expect` as a hint of `tau_next.values`
    (`HintError` if it is not one) and wait for the answer."""
    if not isinstance(text, str):  # pyright: ignore[reportUnnecessaryIsInstance]
        raise TypeError(f"a question is a str, not {type(text).__name__}")
    if to is not None and not isinstance(to, str):  # pyright: ignore[reportUnnecessaryIsInstance]
        raise TypeError(f"`to` is a client id (str) or None, not {type(to).__name__}")
    kind = kind_of(expect)
    if kind is None:
        raise HintError(f"cannot ask for {expect!r}: not a type of tau_next.values")
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

    Every call goes into Rust at once. Nothing here moves a cursor.
    """

    def __init__(
        self,
        session: Session,
        extension: str,
        now: Now,
        poke: Poke,
        changed: Changed,
    ) -> None:
        self._session = session
        self._now = now
        self._poke = poke
        self._changed = changed
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

    def label(self, label: str) -> int:
        """The cursor `label` is on; raises `NoSuchLabel`."""
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

    The tree handle, plus the batch's node and `ask`, which goes to the batch's cursor. Has
    none of the cursor's own operations.
    """

    def __init__(  # noqa: PLR0913, PLR0917 - a handle's whole wiring
        self,
        session: Session,
        id: int,
        extension: str,
        asker: Asker,
        now: Now,
        poke: Poke,
        changed: Changed,
        options: OptionsReader,
    ) -> None:
        super().__init__(session, extension, now, poke, changed)
        self._asker = asker
        self._options = options
        self.id = id

    @property
    def node(self) -> Node:
        return self.tree.node(self.id)

    def options(self) -> Any:
        """This extension's options at the batch's node (its `options` dataclass); raises
        `OptionsError` if it has none or what is set does not fit."""
        return self._options.at(self.extension, self.id)

    def walk(self) -> list[Node]:
        return self.tree.walk(self.id)

    def config(self) -> Config:
        return self.tree.config_at(self.id)

    async def ask(self, text: str, expect: object = str, *, to: str | None = None) -> Any:
        """Ask the batch's cursor's clients `text`, and wait for an answer of type `expect`
        (any hint of `tau_next.values`; `str` by default). Returns the checked, converted
        answer, or `None` if declined. `to` names the client asked (`None`: anyone). The
        call stays out meanwhile, so the cursor is busy; an abort or go-to declines it. A
        `CursorId` answer of `"new"` opens a cursor at the batch's node."""
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
        now: Now,
        poke: Poke,
        changed: Changed,
        options: OptionsReader,
    ) -> None:
        super().__init__(session, extension, now, poke, changed)
        self._writes = HandleWrites(session, id)  # the writes ride this hook's phase
        self._asker = asker
        self._options = options
        self.id = id

    def node(self, id: int | None = None) -> Node:
        return self.tree.node(self.cursor(self.id).at if id is None else id)

    def walk(self) -> list[Node]:
        return self.tree.walk(self.cursor(self.id).at)

    def config(self) -> Config:
        return self.tree.config_at(self.cursor(self.id).at)

    def options(self) -> Any:
        """This extension's options at the cursor's `at` (its `options` dataclass); raises
        `OptionsError` if it has none or what is set does not fit."""
        return self._options.at(self.extension, self.cursor(self.id).at)

    def configure(
        self,
        *,
        model: str | None = None,
        extensions: Mapping[str, Mapping[str, object]] | None = None,
    ) -> int:
        """Write a `Config` node under `at` holding just these fields and move onto it, as
        `write` does: the branch's `model` (a qualified name), and/or `extensions` options
        (`{"step_limit": {"limit": 10}}`), each option replacing the walk's earlier value.
        `OptionsError` for an extension without options or an option that does not fit,
        `ConfigError` if nothing is set. Returns the new node's id."""
        if extensions:
            self._options.check(extensions)
        return self.write(Body.Config(config_node(model, extensions)))

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

    def tell(self, text: str, *, to: str) -> None:
        """Send `text` to client `to` as a `Notice`. Dropped if that client is not connected:
        a notice is for someone who is there."""
        self._check()
        if not isinstance(text, str):  # pyright: ignore[reportUnnecessaryIsInstance]
            raise TypeError(f"a notice is a str, not {type(text).__name__}")
        self._writes.notify(to, text)
        self._changed()

    async def ask(self, text: str, expect: object = str, *, to: str | None = None) -> Any:
        """Ask the cursor's clients `text`, and wait for an answer of type `expect` (any hint
        of `tau_next.values`; `str` by default). Returns the checked, converted answer, or
        `None` if declined. `to` names the client asked (`None`: anyone). The hook's job stays
        out meanwhile, so the cursor is busy; an abort or go-to declines it. A `CursorId`
        answer of `"new"` opens a cursor at the cursor's `at`."""
        self._check()
        return await _ask(self._asker, text, expect, to, lambda: self.open(self.cursor(self.id).at))


@dataclass(frozen=True)
class Handles:
    """What the driver wires into every handle it makes: the session, the question call (by
    cursor), the clock, `poke` and `changed`, and the options. One place that builds them."""

    session: Session
    ask: Callable[..., Awaitable[object]]
    now: Now
    poke: Poke
    changed: Changed
    options: OptionsReader

    def cursor(self, id: int, extension: str) -> Cursor:
        """The handle of cursor `id` for `extension`."""
        return Cursor(
            self.session,
            id,
            extension,
            partial(self.ask, id),
            self.now,
            self.poke,
            self.changed,
            self.options,
        )

    def node(self, cursor: int, id: int, extension: str) -> NodeHandle:
        """The handle of node `id`, the batch of `cursor`, for `extension`."""
        return NodeHandle(
            self.session,
            id,
            extension,
            partial(self.ask, cursor),
            self.now,
            self.poke,
            self.changed,
            self.options,
        )

    def tree(self, extension: str) -> TreeHandle:
        """A handle on no cursor or node, for `extension`."""
        return TreeHandle(self.session, extension, self.now, self.poke, self.changed)

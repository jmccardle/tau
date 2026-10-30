"""What a hook is handed: a cursor or node handle, and the thing it works on."""

from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass

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
    NoSuchLabel,
    NotInHook,
    Session,
    Tree,
)

Poke = Callable[[], None]
"""Tells the driver's pump a handle call may have woken a cursor."""

Changed = Callable[[int | None], None]
"""Tells the driver a handle call changed the tree or a cursor: `Saved` for that cursor, or
for the handle's own cursor (the hook's, or the batch's) when `None`."""

Now = Callable[[], int]
"""The driver's clock, in milliseconds."""

Asker = Callable[[int, str, str], Awaitable[str | None]]
"""The driver's question call: `(cursor, target, question) -> response or None`."""


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
        self._changed(None)
        return node

    def append_root(self, config: Config) -> int:
        """Append a root (a detached tree) holding `config`, without moving any
        cursor. Returns its id."""
        self._check()
        node = self._writes.append_root(config, self.by, self._now())
        self._changed(None)
        return node

    def open(self, at: int) -> int:
        """Open a cursor at `at`; it wakes. Returns its id."""
        self._check()
        cursor = self._writes.open(at, self.by, self._now())
        self._changed(cursor)
        self._poke()
        return cursor

    def add_item(self, to: int, data: str) -> int:
        """Add this extension's item to a cursor's scratchpad; the cursor wakes."""
        self._check()
        item = self._writes.add_item(to, ItemData.Extension(self.extension, data))
        self._changed(to)
        self._poke()
        return item

    def remove_item(self, id: int) -> None:
        """Remove any item; raises `NoSuchItem`. Wakes nothing."""
        self._check()
        owner = next(
            (c for c in self._session.cursors() if any(i.id == id for i in self.cursor(c).items)),
            None,
        )
        self._writes.remove_item(id)
        self._changed(owner)

    def set_label(self, label: str, cursor: int) -> None:
        """Set `label` on `cursor`; a label set elsewhere moves."""
        self._check()
        try:
            was = self._session.label(label)
        except NoSuchLabel:
            was = None
        self._writes.set_label(label, cursor)
        self._changed(cursor)
        if was is not None and was != cursor:
            self._changed(was)

    def remove_label(self, label: str) -> None:
        """Remove `label`; raises `NoSuchLabel`."""
        self._check()
        was = self._session.label(label)
        self._writes.remove_label(label)
        self._changed(was)


class NodeHandle(TreeHandle):
    """What a tool hook acts through: the batch's node, bound to its extension.

    The tree handle, plus the batch's node. Has none of the cursor's own operations.
    """

    def __init__(  # noqa: PLR0913, PLR0917 - a handle's whole wiring
        self, session: Session, id: int, extension: str, now: Now, poke: Poke, changed: Changed
    ) -> None:
        super().__init__(session, extension, now, poke, changed)
        self.id = id

    @property
    def node(self) -> Node:
        return self.tree.node(self.id)

    def walk(self) -> list[Node]:
        return self.tree.walk(self.id)

    def config(self) -> Config:
        return self.tree.config_at(self.id)


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
    ) -> None:
        super().__init__(session, extension, now, poke, changed)
        self._writes = HandleWrites(session, id)  # the writes ride this hook's phase
        self._asker = asker
        self.id = id

    def node(self, id: int | None = None) -> Node:
        return self.tree.node(self.cursor(self.id).at if id is None else id)

    def walk(self) -> list[Node]:
        return self.tree.walk(self.cursor(self.id).at)

    def config(self) -> Config:
        return self.tree.config_at(self.cursor(self.id).at)

    def move(self, to: int) -> None:
        """Move to `to` at once; later hooks see the new `at`. Raises `NoSuchNode`."""
        self._check()
        self._session.hook_move(self.id, to, self.by, self._now())
        self._changed(None)

    def write(self, body: Body, delivers: Sequence[int] = ()) -> int:
        """Append `body` under `at` and move onto it, as one record; the
        `delivers` items are removed. Returns the new node's id."""
        self._check()
        node = self._session.hook_write(self.id, body, list(delivers), self.by, self._now())
        self._changed(None)
        return node

    def set_auto(self, on: bool) -> None:
        """Turn this cursor's auto-step on or off. Wakes nothing: the step is running.
        Turning it on inside a step that is aborted leaves the cursor at rest."""
        self._check()
        self._session.hook_set_auto(self.id, on)
        self._changed(None)

    async def ask(self, target: str, question: str) -> str | None:
        """Wait for the response, or `None` if declined. The hook's job stays out
        meanwhile, so the cursor is busy; an abort or go-to declines it."""
        self._check()
        return await self._asker(self.id, target, question)

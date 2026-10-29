"""What a hook is handed: a cursor or node handle, and the thing it works on."""

from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass

from tau_next._core import (
    Author,
    Body,
    Config,
    Draft,
    Ending,
    Item,
    ItemData,
    Move,
    Node,
    NotInHook,
    Outcome,
    Session,
)

Poke = Callable[[], None]
"""Tells the driver's pump a handle call may have woken a cursor."""

Now = Callable[[], int]
"""The driver's clock, in milliseconds."""

Asker = Callable[[int, str, str], Awaitable[str | None]]
"""The driver's question call: `(cursor, target, question) -> answer or None`."""


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


@dataclass
class CallDraft:
    """The mutable call a tool hook works on.

    Setting `outcome` in `pre_tool` answers the call (the tool does not run);
    in `pre_result` it edits the outcome.
    """

    id: str
    tool: str
    args_raw: str
    outcome: Outcome | None = None


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
        hook: int | None = None,
    ) -> None:
        self._session = session
        self._now = now
        self._poke = poke
        self._hook = hook
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

    def get_node(self, id: int) -> Node:
        return self._session.node(id)

    def lca(self, a: int, b: int) -> int | None:
        return self._session.lca(a, b)

    def cursors(self) -> list[int]:
        return self._session.cursors()

    def cursor_at(self, cursor: int) -> int:
        return self._session.at(cursor)

    def items_of(self, cursor: int) -> list[Item]:
        return self._session.items(cursor)

    def moves_of(self, cursor: int, since: int = 0) -> list[Move]:
        return self._session.moves_of(cursor, since)

    def labels_of(self, cursor: int) -> list[str]:
        return self._session.labels_of(cursor)

    def label(self, label: str) -> int:
        """The cursor `label` is on; raises `NoSuchLabel`."""
        return self._session.label(label)

    def all_labels(self) -> dict[str, int]:
        """Every label and its cursor."""
        return dict(self._session.labels())

    def children(self, node: int) -> list[int]:
        """The children of `node`, in append order."""
        return self._session.children(node)

    def node_count(self) -> int:
        """How many nodes the session has; ids are `0..node_count()`."""
        return self._session.node_count()

    # Writes

    def append(self, parent: int | None, body: Body) -> int:
        """Append a node under `parent` (none: a root, which must be a `Config`)
        without moving any cursor. Returns its id."""
        self._check()
        return self._session.append(parent, body, self.by, self._now(), self._hook)

    def open(self, at: int) -> int:
        """Open a cursor at `at`; it wakes. Returns its id."""
        self._check()
        cursor = self._session.open(at, self.by, self._now(), self._hook)
        self._poke()
        return cursor

    def add_item(self, to: int, data: str) -> int:
        """Add this extension's item to a cursor's scratchpad; the cursor wakes."""
        self._check()
        item = self._session.add_item(
            to, ItemData.Extension(self.extension, data), self._now(), self._hook
        )
        self._poke()
        return item

    def remove_item(self, id: int) -> None:
        """Remove any item; raises `NoSuchItem`. Wakes nothing."""
        self._check()
        self._session.remove_item(id, self._hook)

    def set_label(self, label: str, cursor: int) -> None:
        """Set `label` on `cursor`; a label set elsewhere moves."""
        self._check()
        self._session.set_label(label, cursor, self._hook)

    def remove_label(self, label: str) -> None:
        self._check()
        self._session.remove_label(label, self._hook)


class NodeHandle(TreeHandle):
    """What a tool hook acts through: the batch's node, bound to its extension.

    The tree handle, plus the batch's node. Has none of the cursor's own operations.
    """

    def __init__(self, session: Session, id: int, extension: str, now: Now, poke: Poke) -> None:
        super().__init__(session, extension, now, poke)
        self.id = id

    @property
    def node(self) -> Node:
        return self._session.node(self.id)

    def walk(self) -> list[Node]:
        return self._session.walk(self.id)

    def config(self) -> Config:
        return self._session.config_at(self.id)


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
    ) -> None:
        super().__init__(session, extension, now, poke, hook=id)
        self._asker = asker
        self.id = id

    @property
    def at(self) -> int:
        return self._session.at(self.id)

    @property
    def auto(self) -> bool:
        return self._session.auto(self.id)

    def node(self, id: int | None = None) -> Node:
        return self._session.node(self.at if id is None else id)

    def walk(self) -> list[Node]:
        return self._session.walk(self.at)

    def config(self) -> Config:
        return self._session.config_at(self.at)

    def items(self) -> list[Item]:
        return self._session.items(self.id)

    def labels(self) -> list[str]:
        return self._session.labels_of(self.id)

    def moves(self, *, since_step: bool = False) -> list[Move]:
        """This cursor's move records; with `since_step`, only those since the
        running step woke (other hooks' moves included)."""
        if since_step:
            return self._session.moves_since_step(self.id)
        return self._session.moves_of(self.id)

    def move(self, to: int) -> None:
        """Move to `to` at once; later hooks see the new `at`. Raises `NoSuchNode`."""
        self._check()
        self._session.hook_move(self.id, to, self.by, self._now())

    def write(self, body: Body, delivers: Sequence[int] = ()) -> int:
        """Append `body` under `at` and move onto it, as one record; the
        `delivers` items are removed. Returns the new node's id."""
        self._check()
        return self._session.hook_write(self.id, body, list(delivers), self.by, self._now())

    def set_auto(self, on: bool) -> None:
        """Turn this cursor's auto-step on or off. Wakes nothing: the step is running.
        Turning it on inside a step that is aborted leaves the cursor at rest."""
        self._check()
        self._session.hook_set_auto(self.id, on)

    async def ask(self, target: str, question: str) -> str | None:
        """Wait for the answer, or `None` if declined. The hook's job stays out
        meanwhile, so the cursor is busy; an abort or go-to declines it."""
        self._check()
        return await self._asker(self.id, target, question)

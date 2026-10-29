"""What a hook is handed: a cursor or node handle, and the thing it works on."""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from tau_next._core import Author, Config, Draft, Ending, Item, Node, Outcome, Session

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


class NodeHandle:
    """What a tool hook acts through: the batch's node, bound to its extension.

    Reads the tree. Has none of the cursor's own operations.
    """

    def __init__(self, session: Session, id: int, extension: str) -> None:
        self._session = session
        self.id = id
        self.extension = extension

    @property
    def by(self) -> Author:
        return Author.Extension(self.extension)

    @property
    def node(self) -> Node:
        return self._session.node(self.id)

    def walk(self) -> list[Node]:
        return self._session.walk(self.id)

    def config(self) -> Config:
        return self._session.config_at(self.id)


class Cursor:
    """What a cursor hook acts through: one cursor, bound to its extension.

    Reads the tree and the cursor, and asks. The bindings have no hook-side
    `move` or `write` yet (see the driver's gaps).
    """

    def __init__(self, session: Session, id: int, extension: str, asker: Asker) -> None:
        self._session = session
        self._asker = asker
        self.id = id
        self.extension = extension

    @property
    def by(self) -> Author:
        return Author.Extension(self.extension)

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

    async def ask(self, target: str, question: str) -> str | None:
        """Lock the cursor and wait for the answer, or `None` if declined."""
        return await self._asker(self.id, target, question)

"""The questions extensions have asked and nobody has answered (D16 section 3)."""

import asyncio
import itertools
from collections.abc import Callable
from dataclasses import dataclass, field, replace

from tau_next._values import Context, Kind
from tau_next.events import OpenQuestion


@dataclass
class Question:
    """An open question: what clients see of it (`view`), the future its answer fills, what
    checks and converts an answer (`expect`), and how to open the cursor a `CursorId` answer of
    `"new"` means (`new_cursor`)."""

    view: OpenQuestion
    future: asyncio.Future[object]
    expect: Kind = field(repr=False)
    new_cursor: Callable[[], int] = field(repr=False)


class Questions:
    """The open questions of every cursor. Only a live question (its future not done) can be
    answered or declined; the `ask` that waits on one removes it when it stops waiting, however
    it stopped (an answer, a decline, a timeout, a cancel).

    A question belongs to its cursor, not to a client: it stays open however many clients are
    connected, even none. `to` only says whom it is first for, and only while that client is
    connected: a question for a client that is not (at ask time, or from the moment it
    disconnects) is in the pool, open to everyone, and stays there if the client returns.

    `changed` tells the driver a question opened or closed; `connected` says whether a client
    has a stream open."""

    def __init__(self, changed: Callable[[], None], connected: Callable[[str], bool]) -> None:
        self._by_cursor: dict[int, list[Question]] = {}
        self._aborted: set[int] = set()
        self._ids = itertools.count()
        self._changed = changed
        self._connected = connected

    def open(self, cursor: int) -> tuple[OpenQuestion, ...]:
        """The cursor's open questions, oldest first."""
        return tuple(q.view for q in self._live(cursor))

    def _live(self, cursor: int) -> list[Question]:
        return [q for q in self._by_cursor.get(cursor, []) if not q.future.done()]

    def count(self) -> int:
        return sum(len(self._live(c)) for c in self._by_cursor)

    async def ask(
        self,
        cursor: int,
        text: str,
        expect: Kind,
        to: str | None,
        new_cursor: Callable[[], int],
    ) -> object:
        """Wait for the answer; `None` if declined. Nothing is declined for want of someone to
        answer: the question waits. The one exception is a cursor whose step was aborted: it
        gets `None` at once, and no question is opened, until that step has ended and the cursor
        is at rest."""
        if cursor in self._aborted:
            return None
        if to is not None and not self._connected(to):
            to = None
        future: asyncio.Future[object] = asyncio.get_running_loop().create_future()
        view = OpenQuestion(next(self._ids), to, text, expect.schema)
        question = Question(view, future, expect, new_cursor)
        asked = self._by_cursor.setdefault(cursor, [])
        asked.append(question)
        self._changed()
        try:
            return await future
        finally:
            asked.remove(question)
            if not asked:
                del self._by_cursor[cursor]
            if not future.done():  # withdrawn (a timeout, a cancel), not answered
                self._changed()

    def answer(
        self,
        client: str,
        question: int,
        value: object,
        context: Callable[[Question], Context],
    ) -> None:
        """`client` answers `question` with a decoded JSON value; `None` declines it. The first
        good answer wins: a closed or unknown question is a `LookupError`, a question for
        another client that is connected a `PermissionError` (a client that is not, however it
        was lost, has put its questions in the pool), and a wrong value a `ValueError` naming the
        problem (the question stays open)."""
        found = next(
            (q for c in self._by_cursor for q in self._live(c) if q.view.id == question), None
        )
        if found is None:
            raise LookupError(f"no open question {question}")
        to = found.view.to
        if to is not None and to != client and self._connected(to):  # else it is in the pool
            raise PermissionError(f"question {question} is for {found.view.to!r}, not {client!r}")
        if value is None:
            found.future.set_result(None)
        else:
            where = context(found)
            problem = found.expect.check(value, where)
            if problem is not None:
                raise ValueError(f"the answer {problem}")
            found.future.set_result(found.expect.convert(value, where))
        self._changed()

    def pool(self, client: str) -> None:
        """`client` disconnected: every question open for it is open to everyone now. The
        caller publishes the cursors' changed views."""
        for asked in self._by_cursor.values():
            for question in asked:
                if question.view.to == client:
                    question.view = replace(question.view, to=None)

    def decline(self, cursor: int, *, busy: bool) -> None:
        """The cursor's step is aborted or left: its open questions are declined. If the cursor
        is `busy` a step is still ending, and so is any question it asks until the cursor is at
        rest (`rested`); an idle cursor has no step to end."""
        if busy:
            self._aborted.add(cursor)
        for question in self._live(cursor):
            question.future.set_result(None)

    def rested(self, cursor: int) -> None:
        """`cursor` is at rest: the step an abort ended is over, and the next may ask."""
        self._aborted.discard(cursor)

    def clear(self) -> None:
        self._by_cursor.clear()
        self._aborted.clear()

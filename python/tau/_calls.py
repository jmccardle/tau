"""What a client may do to the driver (D16 section 5): the calls a `LocalClient` makes.

`Calls` holds the driver's session, hub, questions and commands and implements each call of the
`Client` protocol, which documents them; a method here says only what it adds. A call acts for
the client that makes it (`by: Frontend(client)`), runs on the loop thread, publishes what it
changed, and wakes the pump (`frontend`). A client makes each one through `check`, which raises
once the driver has failed or stopped.
"""

import asyncio
from collections.abc import Callable, Collection, Generator, Mapping, Sequence
from contextlib import AbstractContextManager, contextmanager

from tau import trace
from tau._commands import Commands
from tau._config_layers import ModelCatalog
from tau._core import Author, ItemData, Session
from tau._hub import Feed, Hub
from tau._questions import Questions
from tau._values import Context, Form
from tau.events import LiveDelta
from tau.protocols import Clock


def frontend(
    check: Callable[[], None], hub: Hub, poke: Callable[[], None]
) -> Callable[[], AbstractContextManager[None]]:
    """A call that changes the session: `check` first (it raises once the driver has failed or
    stopped: nothing is left to run what the call would start), then the change, then publish
    what changed and wake the pump."""

    @contextmanager
    def changing() -> Generator[None]:
        check()
        yield
        hub.sync()
        poke()

    return changing


class Calls:
    """The calls of the `Client` protocol, on a driver (the module docstring)."""

    def __init__(  # noqa: PLR0913, PLR0917 - what the calls need of the driver
        self,
        session: Session,
        clock: Clock,
        hub: Hub,
        questions: Questions,
        commands: Commands,
        catalog: Callable[[], ModelCatalog],
        *,
        changing: Callable[[], AbstractContextManager[None]],
        check: Callable[[], None],
        cancel: Callable[[int], None],
        catch_up: Callable[[int], Sequence[LiveDelta]],
    ) -> None:
        self.session = session
        self._clock = clock
        self._hub = hub
        self._questions = questions
        self._commands = commands
        self._catalog = catalog
        self._changing = changing
        self.check = check
        self._cancel = cancel  # core said a step's effect is to be cancelled: stop it
        self._catch_up = catch_up

    def connect(self, client: str, buffer: int) -> Feed:
        return self._hub.connect(client, buffer)

    def watch(self, feed: Feed, cursors: Collection[int]) -> None:
        self._hub.watch(feed, cursors, self._catch_up)

    def input(self, cursor: int, client: str, text: str) -> int:
        with trace.span("driver", "input"), self._changing():
            return self.session.input(cursor, Author.Frontend(client), text)

    def send(self, cursor: int, client: str, text: str) -> int:
        with trace.span("driver", "send"), self._changing():
            item = self.session.input(cursor, Author.Frontend(client), text)
            self.session.set_auto(cursor, True)
            return item

    def step(self, cursor: int) -> None:
        with self._changing():
            self.session.step(cursor)

    def go_to(self, cursor: int, to: int, client: str) -> None:
        with self._changing():
            cancel = self.session.go_to(cursor, to, client, self._clock.now())
            self._left(cursor, cancel=cancel)

    def abort(self, cursor: int) -> None:
        with self._changing():
            self._left(cursor, cancel=self.session.abort(cursor))

    def _left(self, cursor: int, *, cancel: bool) -> None:
        """The cursor's step is aborted or left: stop its command, cancel the effect core said
        to cancel, and decline its open questions."""
        self._commands.stop(cursor)
        if cancel:
            self._cancel(cursor)
        self._questions.decline(cursor, busy=self.session.cursor(cursor).busy)

    def set_auto(self, cursor: int, on: bool) -> None:
        with self._changing():
            self.session.set_auto(cursor, on)

    def open(self, at: int, client: str) -> int:
        with self._changing():
            return self.session.open(at, Author.Frontend(client), self._clock.now())

    def add_item(self, to: int, data: ItemData) -> int:
        if isinstance(data, ItemData.Input):
            raise TypeError("an input item is added with `input` or `send`, not `add_item`")
        with self._changing():
            return self.session.add_item(to, data)

    def set_label(self, cursor: int, label: str) -> None:
        with self._changing():
            self.session.set_label(label, cursor)

    def remove_label(self, label: str) -> None:
        with self._changing():
            self.session.remove_label(label)

    def answer(self, client: str, question: int, value: object) -> None:
        self._questions.answer(
            client,
            question,
            value,
            lambda q: Context(self._catalog(), self.session, q.new_cursor),
        )

    def forms(self) -> tuple[Form, ...]:
        return self._commands.forms()

    def run(
        self, client: str, cursor: int, name: str, args: Mapping[str, object]
    ) -> "asyncio.Future[str | None]":
        return self._commands.run(client, cursor, name, args)

    def configure(
        self, client: str, cursor: int, extension: str, values: Mapping[str, object]
    ) -> "asyncio.Future[str | None]":
        return self._commands.configure(client, cursor, extension, values)

    def options(self, cursor: int, extension: str) -> Form:
        return self._commands.options(cursor, extension)

    async def complete(self, cursor: int, named: str, prefix: str) -> list[str]:
        return await self._commands.complete(cursor, named, prefix)

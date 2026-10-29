"""The test Harness: a session and its driver, run in process, shaped like `tau serve`.

No test sleeps. `settle()` yields to the loop until the driver has nothing to do
and every effect in flight is parked at something a test controls.
"""

import asyncio
import functools
from collections.abc import Callable, Coroutine, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fakes import FakeClock, FakeProvider, FakeTool, Gate

from tau_next import (
    Author,
    Body,
    Config,
    Driver,
    Extension,
    Item,
    ItemData,
    Node,
    Session,
    Tool,
)

CLIENT = "term"
PASSES = 10_000


@dataclass(frozen=True)
class CursorState:
    at: int
    walk: tuple[Body, ...]
    items: tuple[Item, ...]
    labels: tuple[str, ...]
    auto: bool
    busy: bool


@dataclass(frozen=True)
class Snapshot:
    """Everything a test may compare: the tree, each cursor, and the notices so far."""

    tree: tuple[tuple[int, int | None, Author, Body], ...]
    cursors: dict[int, CursorState]
    notices: tuple[tuple[str, str], ...]


class Harness:
    def __init__(  # noqa: PLR0913, PLR0917 - one parameter per thing the Harness holds
        self,
        directory: Path,
        session: Session,
        provider: FakeProvider,
        tools: Sequence[Tool],
        extensions: Sequence[Extension],
        clock: FakeClock,
        gates: Sequence[Gate],
    ) -> None:
        self.directory = directory
        self.session = session
        self.provider = provider
        self.tools = tuple(tools)
        self.extensions = tuple(extensions)
        self.clock = clock
        self.gates = tuple(gates)
        self.cursor = session.cursors()[0]
        self._notices: list[tuple[str, str]] = []
        self.driver = self._driver()

    def _driver(self) -> Driver:
        driver = Driver(self.session, self.provider, self.tools, self.extensions, self.clock)
        driver.start()
        return driver

    @classmethod
    async def create(  # noqa: PLR0913 - the Harness's whole setup
        cls,
        tmp_path: Path,
        *,
        config: Config | None = None,
        system: str = "sys",
        provider: FakeProvider | None = None,
        tools: Sequence[Tool] = (),
        extensions: Sequence[Extension] = (),
        clock: FakeClock | None = None,
        gates: Sequence[Gate] = (),
    ) -> "Harness":
        """A new session in `tmp_path`. `gates` are any beyond the fakes' own that
        extensions wait at."""
        clock = clock or FakeClock()
        directory = tmp_path / "session"
        session = Session.create(config or Config("{}"), system, clock.now(), directory)
        return cls(
            directory, session, provider or FakeProvider([]), tools, extensions, clock, gates
        )

    @classmethod
    async def load(  # noqa: PLR0913
        cls,
        tmp_path: Path,
        *,
        provider: FakeProvider | None = None,
        tools: Sequence[Tool] = (),
        extensions: Sequence[Extension] = (),
        clock: FakeClock | None = None,
        gates: Sequence[Gate] = (),
    ) -> "Harness":
        """Open the session `create` left in `tmp_path` (no one holds it)."""
        clock = clock or FakeClock()
        directory = tmp_path / "session"
        session = Session.load(directory, clock.now())
        return cls(
            directory, session, provider or FakeProvider([]), tools, extensions, clock, gates
        )

    # A client's calls (client "term")

    async def input(self, text: str, cursor: int | None = None) -> int:
        return self.driver.input(self._c(cursor), CLIENT, text)

    async def send(self, text: str, cursor: int | None = None) -> int:
        return self.driver.send(self._c(cursor), CLIENT, text)

    async def step(self, cursor: int | None = None) -> None:
        self.driver.step(self._c(cursor), CLIENT)

    async def abort(self, cursor: int | None = None) -> bool:
        return self.driver.abort(self._c(cursor))

    async def go_to(self, to: int, cursor: int | None = None) -> bool:
        return self.driver.go_to(self._c(cursor), to, CLIENT)

    async def set_auto(self, on: bool, cursor: int | None = None) -> None:
        self.driver.set_auto(self._c(cursor), on, CLIENT)

    async def answer(self, answer: str | None, cursor: int | None = None) -> None:
        self.driver.answer(self._c(cursor), answer)

    async def add_item(self, name: str, data: str, cursor: int | None = None) -> int:
        """An extension's item for the cursor: wakes it."""
        return self.driver.add_item(self._c(cursor), ItemData.Extension(name, data))

    def _c(self, cursor: int | None) -> int:
        return self.cursor if cursor is None else cursor

    # Time

    async def settle(self) -> None:
        """Yield until nothing can happen without a test acting.

        Quiet means: the driver has no report or poke to take and no busy
        cursor without an effect out, and every effect in flight is parked at a
        held gate, a fake-clock sleeper, or an open question. Never advances
        the clock. Re-raises what an effect or the pump died of.
        """
        quiet = 0
        for _ in range(PASSES):
            self.driver.check()
            quiet = quiet + 1 if self._quiet() else 0
            if quiet == 2:
                return
            await asyncio.sleep(0)
        raise AssertionError(f"settle: not quiet after {PASSES} passes")

    def _quiet(self) -> bool:
        return not self.driver.queued() and self.driver.units == self.parked()

    def parked(self) -> int:
        gates = {*self.provider.gates, *self.gates}
        gates.update(t.gate for t in self.tools if isinstance(t, FakeTool) and t.gate is not None)
        return sum(g.held for g in gates) + self.clock.sleepers + self.driver.open_questions

    async def crash(self) -> None:
        """Drop memory, keep disk: cancel every task without feeding, close the
        session unsynced, reopen it, and start a new driver on the same fakes."""
        await self.driver.stop()
        self.session.close()
        self.session = Session.load(self.directory, self.clock.now())
        self.driver = self._driver()

    async def close(self) -> None:
        await self.driver.stop()
        self.session.close()

    # Snapshots

    def nodes(self) -> list[Node]:
        return [self.session.node(i) for i in range(self.session.node_count())]

    def bodies(self, cursor: int | None = None) -> list[Body]:
        c = self._c(cursor)
        return [n.body for n in self.session.walk(self.session.at(c))]

    def notices(self) -> list[tuple[str, str]]:
        """Every notice so far, oldest first."""
        self._notices += self.session.take_notices()
        return list(self._notices)

    def snapshot(self) -> Snapshot:
        s = self.session
        return Snapshot(
            tree=tuple((n.id, n.parent, n.by, n.body) for n in self.nodes()),
            cursors={
                c: CursorState(
                    at=s.at(c),
                    walk=tuple(self.bodies(c)),
                    items=tuple(s.items(c)),
                    labels=tuple(s.labels_of(c)),
                    auto=s.auto(c),
                    busy=s.busy(c),
                )
                for c in s.cursors()
            },
            notices=tuple(self.notices()),
        )


def scenario[**P](fn: Callable[P, Coroutine[Any, Any, None]]) -> Callable[P, None]:
    """Make an async test a plain pytest function, run in its own loop."""

    @functools.wraps(fn)
    def run(*args: P.args, **kwargs: P.kwargs) -> None:
        asyncio.run(fn(*args, **kwargs))

    return run

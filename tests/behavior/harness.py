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

from fakes import FAKE_CONFIG, FakeClock, FakeProvider, FakeTool, Gate, fake_catalog

from tau_next import (
    Author,
    Body,
    Config,
    Driver,
    Extension,
    Item,
    ItemData,
    ModelCatalog,
    Node,
    Session,
    Tool,
)

CLIENT = "term"
PASSES = 10_000


@dataclass(frozen=True)
class CursorSeen:
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
    cursors: dict[int, CursorSeen]
    notices: tuple[tuple[str, str], ...]


class Harness:
    def __init__(  # noqa: PLR0913, PLR0917 - one parameter per thing the Harness holds
        self,
        directory: Path,
        session: Session,
        provider: FakeProvider,
        catalog: ModelCatalog,
        tools: Sequence[Tool],
        extensions: Sequence[Extension],
        clock: FakeClock,
        gates: Sequence[Gate],
    ) -> None:
        self.directory = directory
        self.session = session
        self.provider = provider
        self.catalog = catalog
        self.tools = tuple(tools)
        self.extensions = tuple(extensions)
        self.clock = clock
        self.gates = tuple(gates)
        self.cursor = session.cursors()[0]
        self._notices: list[tuple[str, str]] = []
        self.driver = self._driver()

    def _driver(self) -> Driver:
        driver = Driver(
            self.session,
            {self.provider.name: self.provider},
            self.catalog,
            tools=self.tools,
            extensions=self.extensions,
            clock=self.clock,
        )
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
        catalog: ModelCatalog | None = None,
        tools: Sequence[Tool] = (),
        extensions: Sequence[Extension] = (),
        clock: FakeClock | None = None,
        gates: Sequence[Gate] = (),
    ) -> "Harness":
        """A new session in `tmp_path`. `gates` are any beyond the fakes' own that
        extensions wait at."""
        clock = clock or FakeClock()
        directory = tmp_path / "session"
        session = Session.create(config or FAKE_CONFIG, system, clock.now(), directory)
        return cls(
            directory,
            session,
            provider or FakeProvider([]),
            catalog or fake_catalog(),
            tools,
            extensions,
            clock,
            gates,
        )

    @classmethod
    async def load(  # noqa: PLR0913
        cls,
        tmp_path: Path,
        *,
        provider: FakeProvider | None = None,
        catalog: ModelCatalog | None = None,
        tools: Sequence[Tool] = (),
        extensions: Sequence[Extension] = (),
        clock: FakeClock | None = None,
        gates: Sequence[Gate] = (),
    ) -> "Harness":
        """Open the session `create` left in `tmp_path` (no one holds it)."""
        clock = clock or FakeClock()
        directory = tmp_path / "session"
        session = Session.load(directory)
        return cls(
            directory,
            session,
            provider or FakeProvider([]),
            catalog or fake_catalog(),
            tools,
            extensions,
            clock,
            gates,
        )

    # A client's calls (client "term")

    async def input(self, text: str, cursor: int | None = None) -> int:
        return self.driver.input(self._c(cursor), CLIENT, text)

    async def send(self, text: str, cursor: int | None = None) -> int:
        return self.driver.send(self._c(cursor), CLIENT, text)

    async def step(self, cursor: int | None = None) -> None:
        self.driver.step(self._c(cursor))

    async def abort(self, cursor: int | None = None) -> bool:
        return self.driver.abort(self._c(cursor))

    async def go_to(self, to: int, cursor: int | None = None) -> bool:
        return self.driver.go_to(self._c(cursor), to, CLIENT)

    async def set_auto(self, on: bool, cursor: int | None = None) -> None:
        self.driver.set_auto(self._c(cursor), on)

    async def respond(self, response: str | None, cursor: int | None = None) -> None:
        self.driver.respond(self._c(cursor), response)

    async def add_item(self, name: str, data: str, cursor: int | None = None) -> int:
        """An extension's item for the cursor: wakes it. A real extension adds items from
        inside a hook; this stands in for one whose timer fired while the cursor rests."""
        return self.driver.add_item(self._c(cursor), ItemData.Extension(name, data))

    def _c(self, cursor: int | None) -> int:
        return self.cursor if cursor is None else cursor

    # Time

    async def settle(self) -> None:
        """Yield until nothing can happen without a test acting.

        Quiet means: the driver has no report or poke to take and no busy
        cursor without an effect out, and every effect in flight is parked at a
        held gate or an open question. Never advances
        the clock. Re-raises what an effect or the pump died of.
        """
        quiet = 0
        for _ in range(PASSES):
            self.check()
            quiet = quiet + 1 if self._quiet() else 0
            if quiet == 2:
                return
            await asyncio.sleep(0)
        raise AssertionError(f"settle: not quiet after {PASSES} passes")

    def check(self) -> None:
        """Raise what an effect or the pump died of."""
        failure = self.driver.state().failure
        if failure is not None:
            raise failure

    def _quiet(self) -> bool:
        state = self.driver.state()
        return not state.queued and state.units == self.parked()

    def parked(self) -> int:
        gates = {*self.provider.gates, *self.gates}
        gates.update(t.gate for t in self.tools if isinstance(t, FakeTool) and t.gate is not None)
        return sum(g.held for g in gates) + self.driver.state().open_questions

    async def crash(self) -> None:
        """Drop memory, keep disk: cancel every task without answering, close the
        session unsynced, reopen it, and start a new driver on the same fakes."""
        await self.driver.stop()
        self.session.close()
        self.session = Session.load(self.directory)
        self.driver = self._driver()

    async def close(self) -> None:
        await self.driver.stop()
        self.session.close()

    # Snapshots

    def nodes(self) -> list[Node]:
        return [self.session.tree.node(i) for i in range(len(self.session.tree))]

    def bodies(self, cursor: int | None = None) -> list[Body]:
        c = self._c(cursor)
        return [n.body for n in self.session.tree.walk(self.session.cursor(c).at)]

    def notices(self) -> list[tuple[str, str]]:
        """Every notice so far, oldest first."""
        self._notices += self.session.take_notices()
        return list(self._notices)

    def _seen(self, cursor: int) -> CursorSeen:
        state = self.session.cursor(cursor)
        return CursorSeen(
            at=state.at,
            walk=tuple(self.bodies(cursor)),
            items=tuple(state.items),
            labels=tuple(state.labels),
            auto=state.auto,
            busy=state.busy,
        )

    def snapshot(self) -> Snapshot:
        return Snapshot(
            tree=tuple((n.id, n.parent, n.by, n.body) for n in self.nodes()),
            cursors={c: self._seen(c) for c in self.session.cursors()},
            notices=tuple(self.notices()),
        )


def scenario[**P](fn: Callable[P, Coroutine[Any, Any, None]]) -> Callable[P, None]:
    """Make an async test a plain pytest function, run in its own loop."""

    @functools.wraps(fn)
    def run(*args: P.args, **kwargs: P.kwargs) -> None:
        asyncio.run(fn(*args, **kwargs))

    return run

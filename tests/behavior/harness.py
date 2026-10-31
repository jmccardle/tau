"""The test Harness: a session and its driver, run in process, shaped like `tau serve`.

No test sleeps. `settle()` yields to the loop until the driver has nothing to do
and every effect in flight is parked at something a test controls.
"""

import asyncio
import functools
import tempfile
from collections.abc import Callable, Collection, Coroutine, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fakes import FAKE_CONFIG, FakeClock, FakeProvider, FakeTool, Gate, Reply, fake_catalog

from tau_next import (
    Author,
    Body,
    Config,
    CursorView,
    Driver,
    Event,
    Extension,
    Item,
    ItemData,
    LocalClient,
    ModelCatalog,
    Node,
    Notice,
    Part,
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
    notices: tuple[str, ...]


class Harness:
    """A session, its driver, and `client` (id `term`): the calls below are its calls, and
    `heard` is every event it has been sent. Tests that need another client `connect` one."""

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
        self.heard: list[Event] = []
        self.failure: Exception | None = None
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
        self.client = LocalClient(driver, CLIENT)
        self._listener = asyncio.ensure_future(self._listen(self.client))
        return driver

    async def _listen(self, client: LocalClient) -> None:
        """Keep what the client is sent. A stream that ends with the driver's failure ends the
        listening, and `dies` reports the failure."""
        try:
            async for event in client:
                self.heard.append(event)
        except Exception as e:
            self.failure = e

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

    # The client's calls (client "term")

    async def input(self, text: str, cursor: int | None = None) -> int:
        return await self.client.input(self._c(cursor), text)

    async def send(self, text: str, cursor: int | None = None) -> int:
        return await self.client.send(self._c(cursor), text)

    async def step(self, cursor: int | None = None) -> None:
        await self.client.step(self._c(cursor))

    async def abort(self, cursor: int | None = None) -> bool:
        return await self.client.abort(self._c(cursor))

    async def go_to(self, to: int, cursor: int | None = None) -> bool:
        return await self.client.go_to(self._c(cursor), to)

    async def set_auto(self, on: bool, cursor: int | None = None) -> None:
        await self.client.set_auto(self._c(cursor), on)

    async def run(
        self, command: str, args: Mapping[str, object] | None = None, cursor: int | None = None
    ) -> None:
        await self.client.run(self._c(cursor), command, args or {})

    async def respond(self, value: object, cursor: int | None = None) -> None:
        """Answer the cursor's oldest open question."""
        questions = (await self.view(cursor)).questions
        if not questions:
            raise LookupError(f"cursor {self._c(cursor)} has no open question")
        await self.client.answer(questions[0].id, value)

    async def add_item(self, name: str, data: str, cursor: int | None = None) -> int:
        """An extension's item for the cursor: wakes it. A real extension adds items from
        inside a hook; this stands in for one whose timer fired while the cursor rests."""
        return self.driver.add_item(self._c(cursor), ItemData.Extension(name, data))

    async def dies(self) -> Exception:
        """Wait until the driver fails (the client's stream ends with the failure); returns it."""
        await asyncio.wait_for(asyncio.shield(self._listener), 1)
        if self.failure is None:
            raise AssertionError("the client's stream ended without a failure")
        return self.failure

    async def connect(
        self, client: str, *, buffer: int = 256, watching: Collection[int] = ()
    ) -> LocalClient:
        """Another client, connected now and watching `watching`. Close it to disconnect."""
        connected = LocalClient(self.driver, client, buffer=buffer)
        if watching:
            await connected.watch(watching)
        return connected

    async def view(self, cursor: int | None = None) -> CursorView:
        """The cursor as a client connecting now is told of it."""
        target = self._c(cursor)
        async with LocalClient(self.driver, f"{CLIENT}:view") as late:
            async for event in late:
                if isinstance(event, CursorView) and event.cursor == target:
                    return event
        raise AssertionError(f"cursor {target}: no state in the snapshot")

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
        self._listener.cancel()
        await self.driver.stop()
        self.session.close()
        self.session = Session.load(self.directory)
        self.driver = self._driver()

    async def close(self) -> None:
        self._listener.cancel()
        await self.driver.stop()
        self.session.close()

    # Snapshots

    def nodes(self) -> list[Node]:
        return [self.session.tree.node(i) for i in range(len(self.session.tree))]

    def bodies(self, cursor: int | None = None) -> list[Body]:
        c = self._c(cursor)
        return [n.body for n in self.session.tree.walk(self.session.cursor(c).at)]

    def notices(self) -> list[str]:
        """Every notice the client was sent so far, oldest first."""
        return [event.text for event in self.heard if isinstance(event, Notice)]

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


def say(text: str) -> Reply:
    return Reply([Part.Text(text)])


def build(extensions: list[object]) -> None:
    """A driver over `extensions`, built and dropped: what building refuses."""
    session = Session.create(FAKE_CONFIG, "sys", 0, Path(tempfile.mkdtemp()) / "s")
    try:
        Driver(session, {"fake": FakeProvider([])}, fake_catalog(), extensions=extensions)  # pyright: ignore[reportArgumentType]
    finally:
        session.close()


def scenario[**P](fn: Callable[P, Coroutine[Any, Any, None]]) -> Callable[P, None]:
    """Make an async test a plain pytest function, run in its own loop."""

    @functools.wraps(fn)
    def run(*args: P.args, **kwargs: P.kwargs) -> None:
        asyncio.run(fn(*args, **kwargs))

    return run

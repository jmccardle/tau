"""The test Harness: a session and its driver, run in process, shaped like `tau serve`.

No test sleeps. `settle()` yields to the loop until the driver has nothing to do
and every effect in flight is parked at something a test controls.

A Harness has two modes (D21 section 9), set for a whole test by `tests/behavior/conftest.py`:
`local`, where `client`, `connect` and `view` are `LocalClient`s of the driver, and `remote`,
where the Harness serves the driver (`tau.Server`) on a Unix socket in a short temporary
directory and they are `RemoteClient`s of it. What reads the host (the session, `driver.state()`,
snapshots, `add_item`, `crash`) stays the host's. The behavior suite passes in both.
"""

import asyncio
import contextlib
import functools
import shutil
import tempfile
import weakref
from collections.abc import Callable, Collection, Coroutine, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar, Literal

from fakes import FAKE_CONFIG, FakeClock, FakeProvider, FakeTool, Gate, Reply, fake_catalog

from tau import (
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
    Outcome,
    Part,
    RemoteClient,
    Server,
    Session,
    Stop,
    Tool,
    UserConfig,
)

Connected = LocalClient | RemoteClient
"""A client of the Harness's driver, whichever mode: both are a `Client` and a context manager."""

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
    `heard` is every event it has been sent. Tests that need another client `connect` one.
    `Harness.mode` says whether the clients are local or remote (the module docstring)."""

    mode: ClassVar[Literal["local", "remote"]] = "local"

    def __init__(  # noqa: PLR0913, PLR0917 - one parameter per thing the Harness holds
        self,
        directory: Path,
        session: Session,
        provider: FakeProvider,
        catalog: ModelCatalog,
        tools: Sequence[Tool | Callable[..., object]],
        extensions: Sequence[Extension],
        clock: FakeClock,
        gates: Sequence[Gate],
        user_config: UserConfig | None = None,
    ) -> None:
        self.directory = directory
        self.user_config = user_config
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
        self.remote = self.mode == "remote"
        self._remotes: list[RemoteClient] = []
        self._server: Server | None = None
        self._cleanup: Callable[[], object] = lambda: None
        self.socket: Path | None = None

    async def start(self) -> None:
        """Start a driver on the session (`create` and `load` do), and in `remote` mode serve it
        and dial `client`."""
        self.driver = Driver(
            self.session,
            {self.provider.name: self.provider},
            self.catalog,
            tools=self.tools,
            extensions=self.extensions,
            clock=self.clock,
            user_config=self.user_config,
        )
        self.driver.start()
        if self.remote:
            where = Path(tempfile.mkdtemp(prefix="tau"))  # short: a socket path has a limit
            # Removed when the test closes the Harness, and at exit for one that never does.
            self._cleanup = weakref.finalize(self, shutil.rmtree, where, True)
            self.socket = where / "s"
            self._server = await Server.start(
                self.driver, self.socket, session=self.directory.name, cwd=self.directory
            )
        self.client = await self._dial(CLIENT)
        self._listener = asyncio.ensure_future(self._listen(self.client))

    async def _dial(self, client: str, buffer: int = 256, *, flushed: bool = True) -> Connected:
        """A client of the driver: local, or remote (`flushed`: `settle` makes a round trip on
        it)."""
        if self.socket is None:
            return self.driver.client(client, buffer=buffer)
        dialed = await RemoteClient.connect(self.socket, client, buffer=buffer)
        if flushed:
            self._remotes.append(dialed)
        return dialed

    async def _stop_serving(self) -> None:
        """Stop the driver (every stream ends, a remote one after what it holds), then the
        server and the socket's directory."""
        await self.driver.stop()
        if self._server is not None and self.socket is not None:
            await self._server.close()
            self._cleanup()
            self._server = self.socket = None
            self._remotes = []

    async def _listen(self, client: Connected) -> None:
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
        tools: Sequence[Tool | Callable[..., object]] = (),
        extensions: Sequence[Extension] = (),
        clock: FakeClock | None = None,
        gates: Sequence[Gate] = (),
        user_config: UserConfig | None = None,
    ) -> "Harness":
        """A new session in `tmp_path`. `gates` are any beyond the fakes' own that
        extensions wait at; `user_config` is the file the extensions may save options to."""
        clock = clock or FakeClock()
        directory = tmp_path / "session"
        session = Session.create(config or FAKE_CONFIG, system, clock.now(), directory)
        h = cls(
            directory,
            session,
            provider or FakeProvider([]),
            catalog or fake_catalog(),
            tools,
            extensions,
            clock,
            gates,
            user_config,
        )
        await h.start()
        return h

    @classmethod
    async def load(  # noqa: PLR0913
        cls,
        tmp_path: Path,
        *,
        provider: FakeProvider | None = None,
        catalog: ModelCatalog | None = None,
        tools: Sequence[Tool | Callable[..., object]] = (),
        extensions: Sequence[Extension] = (),
        clock: FakeClock | None = None,
        gates: Sequence[Gate] = (),
    ) -> "Harness":
        """Open the session `create` left in `tmp_path` (no one holds it)."""
        clock = clock or FakeClock()
        directory = tmp_path / "session"
        session = Session.load(directory)
        h = cls(
            directory,
            session,
            provider or FakeProvider([]),
            catalog or fake_catalog(),
            tools,
            extensions,
            clock,
            gates,
        )
        await h.start()
        return h

    # The client's calls (client "term")

    async def input(self, text: str, cursor: int | None = None) -> int:
        return await self.client.input(self._c(cursor), text)

    async def send(self, text: str, cursor: int | None = None) -> int:
        return await self.client.send(self._c(cursor), text)

    async def step(self, cursor: int | None = None) -> None:
        await self.client.step(self._c(cursor))

    async def abort(self, cursor: int | None = None) -> None:
        await self.client.abort(self._c(cursor))

    async def go_to(self, to: int, cursor: int | None = None) -> None:
        await self.client.go_to(self._c(cursor), to)

    async def set_auto(self, on: bool, cursor: int | None = None) -> None:
        await self.client.set_auto(self._c(cursor), on)

    async def run(
        self, command: str, args: Mapping[str, object] | None = None, cursor: int | None = None
    ) -> str | None:
        return await self.client.run(self._c(cursor), command, args or {})

    async def configure(
        self,
        extension: str,
        values: Mapping[str, object],
        cursor: int | None = None,
        *,
        save: bool = False,
    ) -> None:
        await self.client.configure(self._c(cursor), extension, values, save=save)

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

    async def send_unattended(self, text: str, cursor: int | None = None) -> int:
        """`send`, as a program that is not connected: `client` is closed, and the host sends
        as `term` and leaves in one act on the loop, so nobody is connected when the cursor
        wakes. (A closed client cannot call; over a wire, the host stands in.)"""
        await self.client.close()
        caller = self.driver.client(CLIENT)
        item = await caller.send(self._c(cursor), text)  # does not yield: the pump has not run
        await caller.close()
        return item

    async def dies(self) -> Exception:
        """Wait until the driver fails (the client's stream ends with the failure); returns it."""
        await asyncio.wait_for(asyncio.shield(self._listener), 1)
        if self.failure is None:
            raise AssertionError("the client's stream ended without a failure")
        return self.failure

    async def connect(
        self, client: str, *, buffer: int = 256, watching: Collection[int] = ()
    ) -> Connected:
        """Another client, connected now and watching `watching`. Close it to disconnect."""
        connected = await self._dial(client, buffer)
        if watching:
            await connected.watch(watching)
        return connected

    async def view(self, cursor: int | None = None) -> CursorView:
        """The cursor as a client connecting now is told of it."""
        target = self._c(cursor)
        late = await self._dial(f"{CLIENT}:view", flushed=False)
        try:
            async for event in late:
                if isinstance(event, CursorView) and event.cursor == target:
                    return event
        finally:
            await late.close()
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
                await self._flush()
                return
            await asyncio.sleep(0)
        raise AssertionError(f"settle: not quiet after {PASSES} passes")

    async def _flush(self) -> None:
        """Remote clients have heard what the driver published: a response arrives after the
        events published before it (D21 section 3), so one round trip each says they are
        queued, and a pass lets `heard` take them."""
        for remote in self._remotes:
            with contextlib.suppress(ConnectionError):  # one that has ended has nothing more
                await remote.node(0)
        await asyncio.sleep(0)

    async def until(self, ready: Callable[[], bool]) -> None:
        """Yield until `ready()`: a call made without awaiting it begins after the loop turns
        that a remote client's request needs (a local one begins in one)."""
        for _ in range(PASSES):
            if ready():
                return
            await asyncio.sleep(0)
        raise AssertionError(f"until: not ready after {PASSES} passes")

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
        state = self.driver.state()
        return sum(g.held for g in gates) + state.parked

    async def crash(self) -> None:
        """Drop memory, keep disk: cancel every task without answering, close the
        session unsynced, reopen it, and start a new driver on the same fakes."""
        self._listener.cancel()
        await self._stop_serving()
        self.session.close()
        self.session = Session.load(self.directory)
        self.heard = []  # the new driver's clients hear it afresh
        await self.start()

    async def close(self) -> None:
        self._listener.cancel()
        await self._stop_serving()
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


def build(extensions: list[object], tools: Sequence[object] = ()) -> None:
    """A driver over `extensions` and `tools`, built and dropped: what building refuses."""
    session = Session.create(FAKE_CONFIG, "sys", 0, Path(tempfile.mkdtemp()) / "s")
    try:
        Driver(
            session,
            {"fake": FakeProvider([])},
            fake_catalog(),
            tools=tools,  # pyright: ignore[reportArgumentType]
            extensions=extensions,  # pyright: ignore[reportArgumentType]
        )
    finally:
        session.close()


async def offered(tmp_path: Path, *tools: object) -> tuple[Tool, ...]:
    """The tools a sample is offered when the driver is given `tools` (functions or `Tool`s)."""
    provider = FakeProvider([Reply([Part.Text("ok")])])
    h = await Harness.create(Path(tempfile.mkdtemp(dir=tmp_path)), provider=provider, tools=tools)  # pyright: ignore[reportArgumentType]
    await h.send("hi")
    await h.settle()
    await h.close()
    return provider.requests[0].tools


async def called(tmp_path: Path, function: object, args_raw: str) -> Outcome:
    """What the model is told when it calls the function `function` with `args_raw`."""
    name: str = function.__name__  # pyright: ignore[reportAttributeAccessIssue,reportUnknownMemberType,reportUnknownVariableType]
    provider = FakeProvider(
        [Reply([Part.Call("c", name, args_raw)], Stop.Tools), Reply([Part.Text("ok")])]
    )
    where = Path(tempfile.mkdtemp(dir=tmp_path))
    h = await Harness.create(where, provider=provider, tools=[function])  # pyright: ignore[reportArgumentType]
    await h.send("go")
    await h.settle()
    results = h.bodies()[-2]
    await h.close()
    assert isinstance(results, Body.Results)
    return results.outcomes[0][1]


SCENARIO_SECONDS = 60
"""How long a scenario may run. No scenario takes a second: one still running at this limit is
waiting for something that will not come, and fails instead of hanging the suite."""

CLEANUP_SECONDS = 5
"""How long the loop gets to cancel what a scenario left running, and to stop its worker threads.
A task that never ends (a cancel made from the wrong thread can leave one) is left behind."""


def _run_bounded(coro: Coroutine[Any, Any, None]) -> None:
    """`asyncio.run`, but neither the scenario nor the loop's cleanup may take for ever:
    `asyncio.run` waits without end for a task that will not cancel."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(asyncio.wait_for(coro, SCENARIO_SECONDS))
    finally:
        try:
            left = asyncio.all_tasks(loop)
            for task in left:
                task.cancel()
            if left:
                loop.run_until_complete(asyncio.wait(left, timeout=CLEANUP_SECONDS))
            loop.run_until_complete(loop.shutdown_asyncgens())
            loop.run_until_complete(
                asyncio.wait_for(loop.shutdown_default_executor(), CLEANUP_SECONDS)
            )
        finally:
            asyncio.set_event_loop(None)
            loop.close()


def scenario[**P](fn: Callable[P, Coroutine[Any, Any, None]]) -> Callable[P, None]:
    """Make an async test a plain pytest function, run in its own loop and bounded by
    `SCENARIO_SECONDS` (`TimeoutError`)."""

    @functools.wraps(fn)
    def run(*args: P.args, **kwargs: P.kwargs) -> None:
        _run_bounded(fn(*args, **kwargs))

    return run

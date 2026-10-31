"""The driver: performs each cursor's jobs and answers the results back.

One pump coroutine per session is the only caller of `next_job` and `answer`
(D11). Core decides every step: which job comes next, what a sample's end
records, what a batch's join is, when a go-to applies. Python owns only the
rules about performing one effect:

- Hooks run in extension order; the first string a hook returns halts the
  rest, and a hook that raises (or breaks its contract) fails the job (a cursor
  hook) or makes the call `Err("Type: msg")` (a tool hook), its traceback on stderr.
- A call's `pre_tool` hooks may answer it before the tool runs, and every
  `pre_tool` hook runs even then; a call that names no tool is
  `Err("unknown tool: ...")`.
- An interrupted call skips `pre_result`.
- A question open when its cursor is aborted or sent elsewhere is declined.
- Every change a client could see is published by one comparison (`Hub.sync`), after each
  call, each pump pass, and each handle write; nothing is announced by hand.
- A failure of the driver cancels every effect in flight; `stop()` cancels
  everything.
"""

import asyncio
import threading
from collections.abc import (
    Awaitable,
    Callable,
    Collection,
    Coroutine,
    Generator,
    Mapping,
    Sequence,
)
from contextlib import contextmanager
from dataclasses import dataclass, field
from functools import partial

from tau_next import trace
from tau_next._builtin import Tau
from tau_next._commands import Commands, assemble_commands
from tau_next._config_layers import ModelCatalog
from tau_next._core import Answer, Author, CursorState, Hook, ItemData, Job, Session
from tau_next._hub import Feed, Hub
from tau_next._options import ExtensionOptions
from tau_next._questions import Questions
from tau_next.clock import SystemClock
from tau_next.effects import CallRun, SampleRun, run_hooks
from tau_next.events import LiveDelta
from tau_next.handles import Handles, Subject
from tau_next.protocols import Clock, Extension, Provider, Tool, check_extensions, check_tools
from tau_next.tools import assemble_tools
from tau_next.values import Context


class _Waiters:
    """Coroutines waiting for the pump to come to rest."""

    def __init__(self) -> None:
        self._futures: list[asyncio.Future[None]] = []

    async def wait(self) -> None:
        future = asyncio.get_running_loop().create_future()
        self._futures.append(future)
        await future

    def wake(self) -> None:
        futures, self._futures = self._futures, []
        for future in futures:
            if not future.done():
                future.set_result(None)


@dataclass
class _Outstanding:
    """One cursor's effect in flight: how to cancel it."""

    sample: SampleRun | None = None
    calls: list[CallRun] = field(default_factory=lambda: [])

    def cancel(self) -> None:
        """Core said abort cancels the effect: stop it. Each call unit and the sample answer
        with what they have."""
        if self.sample is not None:
            self.sample.cancel()
        for call in self.calls:
            call.abort()


class _Tasks:
    """Every task the driver runs: the pump and the effects (`effects`, the ones that answer)."""

    def __init__(self) -> None:
        self.all: set[asyncio.Task[None]] = set()
        self.effects: set[asyncio.Task[None]] = set()

    def spawn(self, coro: Coroutine[object, object, None], *, effect: bool = False) -> None:
        task = asyncio.ensure_future(coro)
        for held in (self.all, self.effects) if effect else (self.all,):
            held.add(task)
            task.add_done_callback(held.discard)


@dataclass(frozen=True)
class DriverState:
    """What a harness reads to tell quiet from busy: the effects running (`units`), whether the
    pump has something to do (`queued`: a report or poke to take, or a busy cursor with no
    effect out; never once the driver has failed or stopped, when the session may be closed),
    the questions open, what the driver died of, and whether it was stopped. `check` raises
    the failure or `RuntimeError` for a stopped driver, as every client call does first."""

    units: int
    queued: bool
    open_questions: int
    failure: Exception | None
    stopped: bool

    def check(self) -> None:
        """Raise the failure the driver died of, or `RuntimeError` if it is stopped."""
        _running(self.failure, self.stopped)


class Driver:
    """Runs one session's cursors over providers, tools, and extensions.

    `providers` maps each provider name to the provider a model with that `provider` is
    sampled by; each must be named by its own `name`. The program that built them closes
    them. `catalog` is where a `Config` node's model name is resolved, at each sample.

    Client calls (`input`, `send`, `step`, `go_to`, `set_auto`, `abort`, `open`, `add_item`,
    `set_label`, `remove_label`, `watch`, `answer`) run on the loop thread, publish what they
    changed, and poke the pump. A frontend makes them through a `Client`, which names itself;
    `connect` opens its stream. Once the driver has failed or stopped they raise (the failure,
    or `RuntimeError`), as `rest` does; a stream connected then ends at once.
    `commands` is the extensions' commands (`/model` and `/config` of the built-in extension
    `tau` first), run for clients; a command that takes a `Cursor` runs ahead of the
    `pre_step` hooks of a step the driver asks for. Aborting or going elsewhere cancels it.
    Extensions are listed in the constructor, in hook order, and checked there like tools
    (`ExtensionContractError`); `model.md` does not yet say how `config_at` names them.
    """

    def __init__(  # noqa: PLR0913 - the driver is given each thing it drives or asks
        self,
        session: Session,
        providers: Mapping[str, Provider],
        catalog: ModelCatalog,
        *,
        tools: Sequence[Tool] = (),
        extensions: Sequence[Extension] = (),
        clock: Clock | None = None,
    ) -> None:
        self.session = session
        self.clock: Clock = clock if clock is not None else SystemClock()
        for name, provider in providers.items():
            if provider.name != name:
                raise ValueError(f"provider {provider.name!r} is given as {name!r}")
        self._providers = dict(providers)
        self._catalog = catalog
        checked = check_extensions(extensions)
        self._options = ExtensionOptions(checked, session, lambda: self._catalog)
        self._extensions = check_extensions([Tau(self._options.names()), *checked])
        self._tools: Mapping[str, Tool]
        listed, self._owners = assemble_tools(tools, self._extensions)
        self._tools, self._progress_tools = check_tools(listed)
        self._queue: asyncio.Queue[tuple[int, Answer] | None] = asyncio.Queue()
        self._out: dict[int, _Outstanding] = {}
        # The hub is built from the questions, so these reach it late.
        self._questions = Questions(
            lambda: self._hub.sync(),  # noqa: PLW0108
            lambda client: self._hub.connected(client),  # noqa: PLW0108
        )
        self._hub = Hub(session, self._questions.open)
        self._tasks = _Tasks()
        self._poke = partial(self._queue.put_nowait, None)  # wake the pump
        # A sync tool or command writes through its handle from a worker thread; what the
        # handle tells the driver goes back to the loop's thread (`start` finds it).
        on_loop = partial(_on_loop, lambda: self._home)
        self._home: tuple[asyncio.AbstractEventLoop, int] | None = None  # the loop, its thread
        self._started_by: dict[int, str | None] = {}  # per cursor, for its current step
        self._handles = Handles(
            session,
            self._questions.ask,
            self.clock.now,
            on_loop(self._poke),
            on_loop(self._hub.sync),
            self._options,
            self._started_by.get,
        )
        self.commands = Commands(
            assemble_commands(self._extensions),
            session=session,
            catalog=lambda: self._catalog,
            options=self._options,
            handles=self._handles,
            hub=self._hub,
            clock=self.clock,
            frontend=self._frontend,
            at_rest=lambda c: not (session.cursor(c).busy or c in self._out),
            spawn=lambda coro: self._tasks.spawn(coro, effect=True),
            on_loop=on_loop,
        )
        self._failure: Exception | None = None
        self._stopped = False
        self._rested = _Waiters()

    def _fail(self, failure: Exception) -> None:
        """Record what the pump or an effect died of, cancel the other effects (they would run
        on, billing, until `stop`), and end every client's stream with it. The first failure is
        the root cause: a later one changes nothing."""
        if self._failure is not None:
            return
        self._failure = failure
        current = asyncio.current_task()
        for effect in tuple(self._tasks.effects):
            if effect is not current:
                effect.cancel()
        self._hub.end(failure)

    def use_catalog(self, catalog: ModelCatalog) -> None:
        """Resolve model names in `catalog` from the next sample dispatched on."""
        self._catalog = catalog

    # Lifecycle

    def start(self) -> None:
        """Start the pump. Call inside the running loop."""
        self._home = (asyncio.get_running_loop(), threading.get_ident())
        self._tasks.spawn(self._pump())

    async def stop(self) -> None:
        """Cancel everything without answering, as a crash would. `rest` and every frontend
        call raise from now on; client streams end, later ones at once."""
        self._stopped = True
        tasks = list(self._tasks.all)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._hub.end()
        self.commands.clear()
        self._out.clear()
        self._questions.clear()
        while not self._queue.empty():
            self._queue.get_nowait()
        self._rested.wake()

    # What a harness needs to tell quiet from busy

    def state(self) -> DriverState:
        """A snapshot of the driver, for a harness."""
        return DriverState(
            units=len(self._tasks.effects),
            queued=self._failure is None
            and not self._stopped
            and (
                not self._queue.empty()
                or any(
                    self.session.cursor(c).busy and c not in self._out
                    for c in self.session.cursors()
                )
            ),
            open_questions=self._questions.count(),
            failure=self._failure,
            stopped=self._stopped,
        )

    async def rest(self, cursor: int) -> None:
        """Wait until the cursor is at rest with no effect out. Raises what an effect or the
        pump died of, and `RuntimeError` once the driver is stopped (also if it is stopped
        meanwhile). A cursor held by an open question or a gate does not rest."""
        while True:
            _running(self._failure, self._stopped)
            if not (self.session.cursor(cursor).busy or cursor in self._out):
                return
            await self._rested.wait()

    # Client calls

    @contextmanager
    def _frontend(self) -> Generator[None]:
        """A call that changes the session: publish what changed and wake the pump. Raises
        once the driver has failed or stopped: nothing is left to run what the call would
        start."""
        _running(self._failure, self._stopped)
        yield
        self._hub.sync()
        self._poke()

    def connect(self, client: str, buffer: int) -> Feed:
        """Open `client`'s stream, now, with its snapshot (`Hub.connect`)."""
        return self._hub.connect(client, buffer)

    def watch(self, feed: Feed, cursors: Collection[int]) -> None:
        """`feed` hears the `Live` of `cursors` and nothing else's, starting with what each
        sample in flight has streamed that is not in the tree yet."""
        _running(self._failure, self._stopped)
        self._hub.watch(feed, cursors, partial(_catch_up, self._out))

    def input(self, cursor: int, client: str, text: str) -> int:
        with trace.span("driver", "input"), self._frontend():
            return self.session.input(cursor, client, text)

    def send(self, cursor: int, client: str, text: str) -> int:
        """Input with auto-step on."""
        with trace.span("driver", "send"), self._frontend():
            item = self.session.input(cursor, client, text)
            self.session.set_auto(cursor, True)
            return item

    def step(self, cursor: int) -> None:
        with self._frontend():
            self.session.step(cursor)

    def go_to(self, cursor: int, to: int, client: str) -> bool:
        """Move, aborting a busy step first (which declines its open questions). True if
        core had an effect in flight to cancel."""
        with self._frontend():
            cancel = self.session.go_to(cursor, to, client, self.clock.now())
            self.commands.stop(cursor)
            if cancel:
                _cancel(self._out, cursor)
            self._questions.decline(cursor)
        return cancel

    def set_auto(self, cursor: int, on: bool) -> None:
        with self._frontend():
            self.session.set_auto(cursor, on)

    def open(self, at: int, client: str) -> int:
        """Open a cursor at `at` for `client`; it wakes."""
        with self._frontend():
            return self.session.open(at, Author.Frontend(client), self.clock.now())

    def add_item(self, to: int, data: ItemData) -> int:
        """Add an item to a cursor's scratchpad; the cursor wakes. For whoever has something
        to tell a resting cursor from outside a hook: a timer extension, `tau serve` relaying a
        webhook. An `ItemData.Input` is what `input` and `send` add (they say who typed it), so
        it is refused here. A hook adds its extension's item with its handle's `add_item`."""
        if isinstance(data, ItemData.Input):
            raise TypeError("an input item is added with `input` or `send`, not `add_item`")
        with self._frontend():
            return self.session.add_item(to, data)

    def set_label(self, label: str, cursor: int) -> None:
        """Put `label` on `cursor`; a label set elsewhere moves."""
        with self._frontend():
            self.session.set_label(label, cursor)

    def remove_label(self, label: str) -> None:
        """Remove `label`; raises `NoSuchLabel`."""
        with self._frontend():
            self.session.remove_label(label)

    def abort(self, cursor: int) -> bool:
        """Abort the cursor's step and decline its open questions. True if core had
        effects in flight to cancel."""
        with self._frontend():
            cancel = self.session.abort(cursor)
            self.commands.stop(cursor)
            if cancel:
                _cancel(self._out, cursor)
            self._questions.decline(cursor)
        return cancel

    def answer(self, client: str, question: int, value: object) -> None:
        """`client` answers `question` with a decoded JSON value; `None` declines it. The
        value is checked against what the question expects and converted: a wrong one raises
        `ValueError` naming the problem and the question stays open; an unknown or closed
        question is a `LookupError`, one for another client a `PermissionError`."""
        _running(self._failure, self._stopped)
        self._questions.answer(
            client,
            question,
            value,
            lambda q: Context(self.session, self._catalog, q.new_cursor),
        )

    # The pump

    async def _pump(self) -> None:
        try:
            while self._failure is None:
                for cursor in self.session.cursors():
                    if cursor not in self._out and self.session.cursor(cursor).busy:
                        with trace.span("driver", "next_job"):
                            job = self.session.next_job(cursor)
                        self._dispatch(cursor, job)
                self._hub.sync()
                self._rested.wake()
                item = await self._queue.get()
                if self._failure is not None:  # a result after the failure is not delivered
                    break
                if item is not None:
                    self._deliver(*item)
        except BaseException as e:
            if isinstance(e, _PASSES):
                raise
            self._fail(_as_failure(e))
        finally:
            self._rested.wake()

    def _deliver(self, cursor: int, answer: Answer) -> None:
        out = self._out.get(cursor)
        if out is None:
            raise RuntimeError(f"cursor {cursor}: a result with no effect out: {answer}")
        with trace.span("driver", "answer"):
            self.session.answer(cursor, answer, self.clock.now())
        if not isinstance(answer, Answer.CallDone):
            del self._out[cursor]
            return
        with trace.span("driver", "next_job"):
            job = self.session.next_job(cursor)  # Pending until the join; then the next job
        if not isinstance(job, Job.Pending):
            del self._out[cursor]
            self._dispatch(cursor, job)

    def _unit(self, cursor: int, effect: Callable[[], Awaitable[Answer]]) -> None:
        """Run an effect as a task that reports `(cursor, Answer)` to the pump."""

        async def unit() -> None:
            try:
                self._queue.put_nowait((cursor, await effect()))
            except BaseException as e:
                if isinstance(e, _PASSES):
                    raise
                self._fail(_as_failure(e))
                self._poke()

        self._tasks.spawn(unit(), effect=True)

    def _dispatch(self, cursor: int, job: Job) -> None:
        session = self.session
        live = partial(self._hub.live, cursor)
        match job:
            case Job.Rest():
                return
            case Job.Hooks():
                at = None if job.node is None else session.tree.node(job.node)
                subject = Subject(draft=job.draft, node=at, ending=job.ending)

                if job.point == Hook.PreStep:
                    self._hub.began(cursor)
                    self._started_by[cursor] = _starter(
                        self.commands.runner(cursor), session.cursor(cursor)
                    )
                elif job.point == Hook.PostStep:
                    if job.ending is None:
                        raise RuntimeError(f"cursor {cursor}: a post_step job with no ending")
                    self._hub.ended(cursor, job.ending)
                failed = partial(_hook_failed, self._hub, cursor)
                effect = partial(
                    run_hooks,
                    job,
                    subject,
                    self._extensions,
                    lambda ext: self._handles.cursor(cursor, ext.name),
                    failed,
                )
                if job.point == Hook.PreStep:
                    effect = self.commands.before(cursor, effect)
                self._out[cursor] = _Outstanding()
                self._unit(cursor, effect)
            case Job.Sample():
                sample = SampleRun(
                    self._providers,
                    self._catalog,
                    session,
                    cursor,
                    tuple(self._tools.values()),
                    live,
                )
                self._out[cursor] = _Outstanding(sample=sample)
                self._unit(cursor, sample.run)
            case Job.RunCalls(node=node, calls=calls):
                if not calls:
                    raise RuntimeError(f"cursor {cursor}: a batch with no calls at node {node}")
                context = Context(
                    session,
                    self._catalog,
                    partial(_open_for_tool, session, self.clock, self._hub, self._poke, node),
                )
                runs = [
                    CallRun(
                        c,
                        lambda owner: self._handles.node(cursor, node, owner),
                        self._tools,
                        self._extensions,
                        context=context,
                        owners=self._owners,
                        batch=node,
                        progress_tools=self._progress_tools,
                        on_progress=live,
                    )
                    for c in calls
                ]
                self._out[cursor] = _Outstanding(calls=runs)
                for run in runs:
                    self._unit(cursor, run.run)
            case _:
                raise RuntimeError(f"cursor {cursor}: a job this driver does not know: {job}")


# What the pump and the effects never take for a failure: the driver's own cancelling, and the
# process being told to go.
_PASSES = (asyncio.CancelledError, KeyboardInterrupt, SystemExit, GeneratorExit)


def _on_loop(
    home: Callable[[], tuple[asyncio.AbstractEventLoop, int] | None], call: Callable[[], None]
) -> Callable[[], None]:
    """`call`, made safe to call from any thread: off the loop's thread it is scheduled on the
    loop (once the driver has started and `home` knows it; before, it is called as it is)."""

    def safe() -> None:
        where = home()
        if where is None or threading.get_ident() == where[1]:
            call()
        else:
            where[0].call_soon_threadsafe(call)

    return safe


def _starter(runner: str | None, state: CursorState) -> str | None:
    """The client a step that begins on this cursor is for: the runner of the command that asked
    for it, else the client of the newest input the step is about to deliver, else none."""
    if runner is not None:
        return runner
    clients = [i.data.client for i in state.items if isinstance(i.data, ItemData.Input)]
    return clients[-1] if clients else None


def _as_failure(error: BaseException) -> Exception:
    """What the driver failed of. Core's panic reaches Python as PyO3's `PanicException`, a
    `BaseException`; it is wrapped so every failure is an `Exception`."""
    if isinstance(error, Exception):
        return error
    failure = RuntimeError(f"{type(error).__name__}: {error}")
    failure.__cause__ = error
    return failure


def _running(failure: Exception | None, stopped: bool) -> None:
    """Raise the failure the driver died of, or that it is stopped."""
    if failure is not None:
        raise failure
    if stopped:
        raise RuntimeError("the driver is stopped")


def _catch_up(outs: dict[int, _Outstanding], cursor: int) -> Sequence[LiveDelta]:
    """What the sample in flight for `cursor` has streamed that is not in the tree yet."""
    out = outs.get(cursor)
    return [] if out is None or out.sample is None else out.sample.catch_up()


def _cancel(outs: dict[int, _Outstanding], cursor: int) -> None:
    """Core said abort cancels the effect out for `cursor`."""
    out = outs.get(cursor)
    if out is None:
        raise RuntimeError(f"cursor {cursor}: abort cancels an effect, but none is out")
    out.cancel()


def _open_for_tool(
    session: Session, clock: Clock, hub: Hub, poke: Callable[[], None], node: int
) -> int:
    """Open a cursor at `node` for a tool argument of `"new"`."""
    cursor = session.open(node, Author.Tools(), clock.now())
    hub.sync()
    poke()
    return cursor


def _hook_failed(hub: Hub, cursor: int, point: str, extension: str, error: str) -> None:
    """A cursor hook's failure, as the `hook_error` of the cursor it ran for."""
    hub.hook_failed(cursor, point, extension, error)

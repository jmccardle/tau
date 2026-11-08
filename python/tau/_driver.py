"""The driver: performs each cursor's jobs and answers the results back.

One pump coroutine per session is the only caller of `next_job` and `answer`
(D11). Core decides every step: which job comes next, what a sample's end
records, what a batch's join is, when a go-to applies. Python owns only the
rules about performing one effect:

- Hooks run in extension order; the first string a hook returns halts the
  rest, and a hook that raises (or breaks its contract) fails the job (a cursor
  hook) or makes the call `Err("Type: msg")` (a tool hook), its traceback on stderr (`tau -p`
  shows it only with `--verbose`).
- A call's `pre_tool` hooks may answer it before the tool runs, and every
  `pre_tool` hook runs even then; a call that names no tool is
  `Err("unknown tool: ...")`.
- An interrupted call skips `pre_result`.
- A question open when its cursor is aborted or sent elsewhere is declined, and so is one asked
  later in that step; a question never waits on a client being there (D16 section 3).
- A sample is offered the tools its `pre_sample` hooks left in `subject.tools` (all if none did),
  and a call it makes to a registered tool outside the offer is `Err("tool not offered: X")`
  without running any hook (a reply this driver did not sample is not checked).
- Every change a client could see is published by one comparison (`Hub.sync`), after each
  call, each pump pass, and each handle write; nothing is announced by hand.
- A failure of the driver cancels every effect in flight; `stop()` cancels
  everything.
"""

import asyncio
import concurrent.futures
import contextvars
import threading
from collections import Counter
from collections.abc import (
    Awaitable,
    Callable,
    Coroutine,
    Generator,
    Mapping,
    Sequence,
)
from contextlib import contextmanager
from dataclasses import dataclass, field
from functools import partial

from tau import trace
from tau._builtin import Tau
from tau._calls import Calls, frontend
from tau._client import LocalClient
from tau._clock import SystemClock
from tau._commands import Commands, assemble_commands
from tau._config import ConfigError
from tau._config_layers import ModelCatalog
from tau._core import Answer, Author, Busy, Ending, Hook, ItemData, Job, Session
from tau._effects import CallRun, SampleRun, run_hooks
from tau._events import LiveDelta
from tau._handles import Handles, Subject
from tau._hub import Hub
from tau._meter import Counts
from tau._options import ExtensionOptions
from tau._protocols import (
    Clock,
    Extension,
    Provider,
    Rescanning,
    Tool,
    check_extensions,
    check_tools,
    counter_of,
)
from tau._questions import Questions
from tau._tools import assemble_tools
from tau._user_config import UserConfig
from tau._values import Context, Kind


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


class _Leavers:
    """The waits on other cursors, by the cursor that waits: a hook's `rest` ends when its own
    cursor is aborted or sent elsewhere, since an abort never cancels a hook (M7)."""

    def __init__(self, wake: Callable[[], None]) -> None:
        self._wake = wake  # the waits are woken to see they ended
        self._waiting: Counter[int] = Counter()
        self._gone: set[int] = set()

    @contextmanager
    def waiting(self, cursor: int) -> Generator[None]:
        """`cursor` has a wait out."""
        self._waiting[cursor] += 1
        try:
            yield
        finally:
            self._waiting[cursor] -= 1
            if not self._waiting[cursor]:
                del self._waiting[cursor]
                self._gone.discard(cursor)

    def left(self, cursor: int) -> None:
        """`cursor` was aborted or sent elsewhere: a wait of it that is out ends."""
        if cursor in self._waiting:
            self._gone.add(cursor)
            self._wake()

    def has_left(self, cursor: int) -> bool:
        return cursor in self._gone


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


_EFFECT: contextvars.ContextVar[asyncio.Task[None] | None] = contextvars.ContextVar(
    "effect", default=None
)
"""The effect task the running code belongs to, tasks it starts included."""


class _Tasks:
    """Every task the driver runs: the pump and the effects (`effects`, the ones that answer)."""

    def __init__(self) -> None:
        self.all: set[asyncio.Task[None]] = set()
        self.effects: set[asyncio.Task[None]] = set()

    def spawn(self, coro: Coroutine[object, object, None], *, effect: bool = False) -> None:
        run = coro
        if effect:

            async def run_as_effect() -> None:
                # Inside the task, so its first step sees it, even when the loop's task factory
                # starts a task eagerly (Textual's does) before `create_task` returns.
                task = asyncio.current_task()
                _EFFECT.set(task)
                await coro

            run = run_as_effect()
        task = asyncio.create_task(run)
        if effect:  # a task cancelled before its first step never started `coro`: close it
            task.add_done_callback(lambda _: coro.close())
        for held in (self.all, self.effects) if effect else (self.all,):
            held.add(task)
            task.add_done_callback(held.discard)


@dataclass(frozen=True)
class DriverState:
    """What a harness reads to tell quiet from busy: the effects running (`units`), whether the
    pump has something to do (`queued`: a report or poke to take, or a busy cursor with no
    effect out; never once the driver has failed or stopped, when the session may be closed),
    the questions open, the effects parked in the driver (`parked`: an effect with a question
    open or a handle's `rest` or `close_cursor` out waits for an answer or another cursor, and
    counts once however many it has out), what the driver died of, and whether it was stopped.
    `check` raises `ConnectionError` for a failed or stopped driver, as every client call does
    first."""

    units: int
    queued: bool
    open_questions: int
    parked: int
    failure: Exception | None
    stopped: bool

    def check(self) -> None:
        """Raise `ConnectionError` if the driver has failed or is stopped."""
        _running(self.failure, self.stopped)


class Driver:
    """Runs one session's cursors over providers, tools, and extensions.

    `providers` maps each provider name to the provider a model with that `provider` is
    sampled by; each must be named by its own `name`. The program that built them closes
    them. `catalog` is where a `Config` node's model name is resolved, at each sample.
    `user_config` (`LoadedConfig.user`) is what lets an extension save its options to the user's
    file (`Cursor.save_options`, `/config --save`); without it they raise `ConfigError`.

    A frontend never calls the driver: it holds a `Client` (`client(id)` makes one, naming
    itself), whose calls run on the loop thread, publish what they changed, and poke the pump.
    Once the driver has failed or stopped they raise `ConnectionError` (the failure's
    `"Type: msg"`, chained, or "the driver is stopped"), as `rest` does; a stream connected then
    ends at once. The extensions' commands (`/model` of the
    built-in extension `tau` first) run for clients; a command that takes a `Cursor` runs ahead
    of the `pre_step` hooks of a step the driver asks for. Aborting or going elsewhere cancels
    it. Extensions are listed in the constructor, in hook order, and checked there like tools
    (`DefinitionError`); `model.md` does not yet say how `config_at` names them. `reload` swaps
    them for others while nothing is in flight.
    """

    def __init__(  # noqa: PLR0913 - the driver is given each thing it drives or asks
        self,
        session: Session,
        providers: Mapping[str, Provider],
        catalog: ModelCatalog,
        *,
        tools: Sequence[Tool | Callable[..., object]] = (),
        extensions: Sequence[Extension] = (),
        clock: Clock | None = None,
        user_config: UserConfig | None = None,
    ) -> None:
        self.session = session
        self.clock: Clock = clock if clock is not None else SystemClock()
        self._failure: Exception | None = None
        self._stopped = False
        for name, provider in providers.items():
            if provider.name != name:
                raise ValueError(f"provider {provider.name!r} is given as {name!r}")
        self._providers = dict(providers)
        self._catalog = catalog
        self._given = tuple(tools)
        # cursor -> the offer of its last pre_sample that a sample follows (None: every tool)
        self._offered: dict[int, tuple[str, ...] | None] = {}
        self._made_with: dict[int, tuple[int, tuple[str, ...]]] = {}  # cursor -> (at, offer)
        self._options = ExtensionOptions(
            (),
            session,
            lambda: self._catalog,
            user_config,
            self.use_catalog,
            tool_names=lambda: self._tools,
        )
        self._queue: asyncio.Queue[tuple[int, Answer] | None] = asyncio.Queue()
        self._out: dict[int, _Outstanding] = {}
        # The hub is built from the questions, so these reach it late.
        self._questions = Questions(
            lambda: self._hub.sync(),  # noqa: PLW0108
            lambda client: self._hub.connected(client),  # noqa: PLW0108
        )
        self._rested = _Waiters()
        self._leavers = _Leavers(self._rested.wake)
        self._hub = Hub(session, self._questions.open, self._questions.pool, self._model_at)
        self._tasks = _Tasks()
        self._poke = partial(self._queue.put_nowait, None)  # wake the pump
        # A sync tool or command writes through its handle from a worker thread; what the
        # handle tells the driver goes back to the loop's thread (`start` finds it).
        on_loop = partial(_on_loop, lambda: self._home)
        self._home: tuple[asyncio.AbstractEventLoop, int] | None = None  # the loop, its thread
        self._handles = Handles(
            session,
            self._ask,
            lambda client, text: on_loop(partial(self._hub.notify, client, text))(),
            self.clock.now,
            on_loop(self._poke),
            on_loop(self._hub.sync),
            self._options,
            self._ending_at_rest,
            lambda cursor: _on_loop_wait(lambda: self._home, partial(self._calls.abort, cursor)),
            self._close_cursor,
            lambda cursor, extension, text: _on_loop_wait(
                lambda: self._home, partial(self._hub.show, cursor, extension, text)
            ),
            lambda: tuple(self._tools),
            Counts(
                session.tree.node,
                self._options.model,
                lambda: tuple(self._tools),
                lambda: counter_of(self._extensions),
            ),
        )
        self._changing = frontend(self._check, self._hub, self._poke)
        self._commands = Commands(
            {},
            session=session,
            catalog=lambda: self._catalog,
            options=self._options,
            handles=self._handles,
            hub=self._hub,
            clock=self.clock,
            frontend=self._changing,
            at_rest=lambda c: not (session.cursor(c).busy or c in self._out),
            spawn=lambda coro: self._tasks.spawn(coro, effect=True),
            on_loop=on_loop,
            rescan=lambda: _rescan(self._extensions, self._commands),
        )
        self._calls = Calls(
            session,
            self.clock,
            self._hub,
            self._questions,
            self._commands,
            lambda: self._catalog,
            changing=self._changing,
            check=self._check,
            cancel=partial(_cancel, self._out),
            catch_up=partial(_catch_up, self._out),
            rest=self.rest,
            left=self._leavers.left,
        )
        self._waits: Counter[asyncio.Task[None]] = (
            Counter()
        )  # effect -> waits on cursors it has out
        self._adopt(extensions)

    def _adopt(self, extensions: Sequence[Extension]) -> None:
        """Make `extensions` (and the driver's own `tools`) what the driver runs from the next
        job it dispatches. `DefinitionError`, with nothing changed, as at construction."""
        checked = check_extensions(extensions)
        every = check_extensions([Tau(), *checked])
        listed, owners = assemble_tools(self._given, every)
        tools, progress_tools = check_tools(listed)
        commands = assemble_commands(every)
        self._options.reload(checked)
        self._hub.keep_shown([e.name for e in every])
        self._commands.use(commands)
        self._extensions, self._tools, self._owners = every, tools, owners
        self._progress_tools = progress_tools

    def _model_at(self, node: int) -> str | None:
        """The model the branch at `node` is on, for a `CursorView`: `None` if no `Config` on it
        names one or one is unreadable (a sample fails with why)."""
        try:
            return self._options.model(node)
        except ConfigError:
            return None

    def _check(self) -> None:
        """Raise `ConnectionError` if the driver has failed or is stopped."""
        _running(self._failure, self._stopped)

    def _fail(self, failure: Exception) -> None:
        """Record what the pump or an effect died of, cancel the other effects (they would run
        on, billing, until `stop`), and end every client's stream with it. The first failure is
        the root cause: a later one changes nothing."""
        if self._failure is not None:
            return
        self._failure = failure
        self._commands.clear()
        current = asyncio.current_task()
        for effect in tuple(self._tasks.effects):
            if effect is not current:
                effect.cancel()
        self._hub.end(failure)

    def use_catalog(self, catalog: ModelCatalog) -> None:
        """Resolve model names in `catalog` from the next sample dispatched on."""
        self._catalog = catalog

    def reload(self, extensions: Sequence[Extension]) -> None:
        """Run `extensions` (listed in hook order, as at construction) instead of the ones the
        driver has, with the same `tools`: rebuild the options, tools, tool owners and commands,
        and forget every folded `Config`. Only when nothing is in flight: no cursor busy (a
        command waiting for its step has none out yet, but its cursor is busy) and no effect
        out, since every job holds the set it was dispatched with (`Busy` naming the cursors);
        the next job dispatched uses the new one. `DefinitionError` (nothing changes) as at
        construction. The tree is not rewritten: a `Config` node that sets options of an
        extension no longer here (or no longer fitting its type) fails when it is read
        (`ConfigError`), not now. Clients are told nothing, as after `use_catalog`: they ask
        `commands()` and `options()` again."""
        self._check()
        busy = sorted(
            {c for c in self.session.cursors() if self.session.cursor(c).busy} | set(self._out)
        )
        if busy or self._tasks.effects:
            raise Busy(
                f"cannot reload while work is out: cursors {busy}, "
                f"{len(self._tasks.effects)} effects running"
            )
        self._adopt(extensions)
        self._hub.sync()  # what an extension that is gone showed

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
        self._commands.clear()
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
            parked=len(self._questions.askers() | self._waits.keys()),
            failure=self._failure,
            stopped=self._stopped,
        )

    async def rest(self, cursor: int, waiter: int | None = None) -> None:
        """Wait until the cursor is at rest with no effect out. Raises `ConnectionError` once the
        driver has failed or stopped (also if it does meanwhile). A cursor held by an open
        question or a gate does not rest. Returns early if `waiter`, the cursor that waits (a
        handle's), is aborted or left meanwhile."""
        while True:
            _running(self._failure, self._stopped)
            if not (self.session.cursor(cursor).busy or cursor in self._out):
                return
            if waiter is not None and self._leavers.has_left(waiter):
                return
            await self._rested.wait()

    @staticmethod
    def _effect() -> asyncio.Task[None]:
        """The running effect. Only a handle asks or waits, and a handle is only ever in an
        effect's hands."""
        effect = _EFFECT.get()
        if effect is None:
            raise RuntimeError("a handle was used outside an effect: a handle is not for keeping")
        return effect

    @contextmanager
    def _waiting(self) -> Generator[None]:
        """The running effect waits on another cursor: `state` counts it parked, once however
        many such waits it has out."""
        effect = self._effect()
        self._waits[effect] += 1
        try:
            yield
        finally:
            self._waits[effect] -= 1
            if not self._waits[effect]:
                del self._waits[effect]

    async def _ask(  # noqa: PLR0913, PLR0917 - a question's whole description
        self,
        cursor: int,
        extension: str,
        text: str,
        expect: Kind,
        to: str | None,
        new_cursor: Callable[[], int],
    ) -> object:
        """A handle's question (`Questions.ask`), for the running effect."""
        return await self._questions.ask(
            cursor, extension, text, expect, to, new_cursor, self._effect()
        )

    async def _ending_at_rest(self, cursor: int, waiter: int | None) -> Ending | None:
        """`rest`, then how the cursor's last step ended (`None` if not known): what a handle's
        `rest` returns. `Ending.Aborted()` instead if `waiter`, the cursor that waits, is aborted
        or sent elsewhere first."""
        with self._waiting():
            if waiter is None:
                await self.rest(cursor)
            else:
                with self._leavers.waiting(waiter):
                    await self.rest(cursor, waiter)
                    if self._leavers.has_left(waiter):
                        return Ending.Aborted()
        return self._hub.ending(cursor)

    async def _close_cursor(self, cursor: int) -> None:
        """What a handle's `close_cursor` does: the close, its effect parked while it waits."""
        with self._waiting():
            await self._calls.close_cursor(cursor)

    # What a frontend uses

    def client(self, id: str, *, buffer: int = 256) -> LocalClient:
        """A client named `id` (stable across reconnects: `john@laptop:tui`), connected now, its
        stream opening with the snapshot: make it inside the running loop. `buffer` bounds the
        stream (`Feed`)."""
        return LocalClient(self._calls, id, buffer)

    def add_item(self, to: int, data: ItemData) -> int:
        """Add an item to a cursor's scratchpad; the cursor wakes. For whoever has something
        to tell a resting cursor from outside a hook: a timer extension, `tau serve` relaying a
        webhook. An `ItemData.Input` is what a client's `input` and `send` add (they say who
        typed it), so it is refused here. A hook adds its extension's item with its handle's
        `add_item`."""
        return self._calls.add_item(to, data)

    # The pump

    async def _pump(self) -> None:
        try:
            while self._failure is None:
                for cursor in self.session.cursors():
                    if cursor in self._out:
                        continue
                    if not self.session.cursor(cursor).busy:
                        self._questions.rested(cursor)
                        continue
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

    def _offering(
        self, cursor: int, subject: Subject, hooks: Callable[[], Awaitable[Answer]]
    ) -> Callable[[], Awaitable[Answer]]:
        """The effect of a `pre_sample` job: `hooks`, then what they left in `subject.tools` is
        the offer of the sample that follows. Every answer replaces the last offer; one that no
        sample follows (a halt, a draft that answers the sample, a failed hook) leaves none."""

        async def effect() -> Answer:
            answer = await hooks()
            if isinstance(answer, Answer.Hooks) and answer.stop is None and answer.draft is None:
                self._offered[cursor] = None if subject.tools is None else tuple(subject.tools)
            else:
                self._offered.pop(cursor, None)
            return answer

        return effect

    def _hooks(self, cursor: int, job: Job.Hooks) -> Callable[[], Awaitable[Answer]]:
        """The effect of a hooks job: the extensions' hooks for its point, with what the point
        needs of the driver around them (a step's ending, the offer of a sample, a command)."""
        at = None if job.node is None else self.session.tree.node(job.node)
        subject = Subject(draft=job.draft, node=at, ending=job.ending)
        if job.point == Hook.PreStep:
            self._hub.began(cursor)
        elif job.point == Hook.PostStep:
            if job.ending is None:
                raise RuntimeError(f"cursor {cursor}: a post_step job with no ending")
            self._hub.ended(cursor, job.ending)
        effect = partial(
            run_hooks,
            job,
            subject,
            self._extensions,
            lambda ext: self._handles.cursor(cursor, ext.name),
            partial(_hook_failed, self._hub, cursor),
            self._tools,
        )
        if job.point == Hook.PreSample:
            return self._offering(cursor, subject, effect)
        if job.point == Hook.PreStep:
            return self._commands.before(cursor, effect)
        return effect

    def _offered_tools(self, cursor: int) -> tuple[Tool, ...]:
        """The tools a sample of `cursor` is offered: its `pre_sample` hooks' choice, once."""
        offered = self._offered.pop(cursor, None)
        tools = tuple(t for n, t in self._tools.items() if offered is None or n in offered)
        self._made_with[cursor] = (self.session.cursor(cursor).at, tuple(t.name for t in tools))
        return tools

    def _dispatch(self, cursor: int, job: Job) -> None:
        session = self.session
        live = partial(self._hub.live, cursor)
        match job:
            case Job.Rest():
                return
            case Job.Hooks():
                self._out[cursor] = _Outstanding()
                self._unit(cursor, self._hooks(cursor, job))
            case Job.Sample():
                sample = SampleRun(
                    self._providers,
                    self._catalog,
                    session,
                    cursor,
                    self._offered_tools(cursor),
                    live,
                    self._options.model,
                )
                self._out[cursor] = _Outstanding(sample=sample)
                self._unit(cursor, sample.run)
            case Job.RunCalls(node=node, calls=calls):
                if not calls:
                    raise RuntimeError(f"cursor {cursor}: a batch with no calls at node {node}")
                context = Context(
                    self._catalog,
                    session,
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
                        offered=_offer_of(
                            self._made_with.get(cursor), session.tree.node(node).parent
                        ),
                    )
                    for c in calls
                ]
                self._out[cursor] = _Outstanding(calls=runs)
                for run in runs:
                    self._unit(cursor, run.run)
            case _:
                raise RuntimeError(f"cursor {cursor}: a job this driver does not know: {job}")


def _offer_of(
    made: tuple[int, tuple[str, ...]] | None, parent: int | None
) -> tuple[str, ...] | None:
    """The names of the tools offered to the sample that wrote a reply whose parent is `parent`,
    if this driver took that sample from `made` (its cursor's last: where it began, what it was
    offered). A reply from before a reload, or one a cursor went back to, is not known."""
    return made[1] if made is not None and made[0] == parent else None


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


def _on_loop_wait[T](
    home: Callable[[], tuple[asyncio.AbstractEventLoop, int] | None], call: Callable[[], T]
) -> T:
    """`call()` on the loop's thread, whichever thread asks, and its result (or what it
    raises) returned to the asker, who waits for it. Unlike `_on_loop` this is for a call whose
    effect the asker's next step depends on (an abort made before a `send` must come first) or
    whose refusal the asker must hear (a `show` on a cursor that is gone).
    `RuntimeError` if the loop is closed; the asker waits for as long as the loop is not run."""
    where = home()
    if where is None or threading.get_ident() == where[1]:
        return call()
    loop = where[0]
    done: concurrent.futures.Future[T] = concurrent.futures.Future()

    def run() -> None:
        try:
            done.set_result(call())
        except BaseException as e:
            done.set_exception(e)

    loop.call_soon_threadsafe(run)
    return done.result()


def _as_failure(error: BaseException) -> Exception:
    """What the driver failed of. Core's panic reaches Python as PyO3's `PanicException`, a
    `BaseException`; it is wrapped so every failure is an `Exception`."""
    if isinstance(error, Exception):
        return error
    failure = RuntimeError(f"{type(error).__name__}: {error}")
    failure.__cause__ = error
    return failure


def _running(failure: Exception | None, stopped: bool) -> None:
    """Raise `ConnectionError` if the driver has failed (`"Type: msg"` of the failure, chained)
    or stopped: a client's stream ends with the same error, so a frontend has one rule."""
    if failure is not None:
        raise ConnectionError(f"{type(failure).__name__}: {failure}") from failure
    if stopped:
        raise ConnectionError("the driver is stopped")


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


def _rescan(extensions: Sequence[Extension], commands: Commands) -> None:
    """Ask each extension that has a `rescan` (the commands it lists come from files) whether its
    commands changed, and run the new ones if any did."""
    changed = [e.rescan() for e in extensions if isinstance(e, Rescanning)]
    if any(changed):
        commands.use(assemble_commands(extensions))


def _hook_failed(hub: Hub, cursor: int, point: str, extension: str, error: str) -> None:
    """A cursor hook's failure, as the `hook_error` of the cursor it ran for."""
    hub.hook_failed(cursor, point, extension, error)

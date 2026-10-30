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
- A failure of the driver cancels every effect in flight; `stop()` cancels
  everything.
"""

import asyncio
import weakref
from collections.abc import Awaitable, Callable, Coroutine, Generator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from functools import partial
from typing import cast

from tau_next import trace
from tau_next._config_layers import ModelCatalog
from tau_next._core import Answer, Author, Body, ItemData, Job, Session
from tau_next.clock import SystemClock
from tau_next.effects import CallRun, SampleRun, run_hooks
from tau_next.events import CallKey, Event, EventStream, HookError, Live, LiveDelta, Saved
from tau_next.handles import Changed, Cursor, NodeHandle, Subject
from tau_next.protocols import Clock, Extension, Provider, Tool, check_extensions, check_tools


@dataclass
class Question:
    """An open question: who it is for, what it asks, and the future its response fills."""

    target: str
    text: str
    future: asyncio.Future[str | None]


class _Questions:
    """The open questions of every cursor. Only a live question (its future not done) can be
    responded to or declined; the `ask` that waits on one removes it when it stops waiting,
    however it stopped (a response, a decline, a timeout, a cancel)."""

    def __init__(self) -> None:
        self._by_cursor: dict[int, list[Question]] = {}

    def open(self, cursor: int) -> list[Question]:
        return [q for q in self._by_cursor.get(cursor, []) if not q.future.done()]

    def count(self) -> int:
        return sum(len(self.open(c)) for c in self._by_cursor)

    async def ask(self, cursor: int, target: str, text: str) -> str | None:
        question = Question(target, text, asyncio.get_running_loop().create_future())
        asked = self._by_cursor.setdefault(cursor, [])
        asked.append(question)
        try:
            return await question.future
        finally:
            asked.remove(question)
            if not asked:
                del self._by_cursor[cursor]

    def respond(self, cursor: int, response: str | None) -> None:
        open_ = self.open(cursor)
        if not open_:
            raise LookupError(f"cursor {cursor} has no open question")
        open_[0].future.set_result(response)

    def decline(self, cursor: int) -> None:
        for question in self.open(cursor):
            question.future.set_result(None)

    def clear(self) -> None:
        self._by_cursor.clear()


class _Streams:
    """The event streams open now. Held weakly: a stream abandoned without `aclose()` is
    collected."""

    def __init__(self) -> None:
        self._open: weakref.WeakSet[EventStream] = weakref.WeakSet()
        self._ended: tuple[Exception | None] | None = None

    def __len__(self) -> int:
        return len(self._open)

    def subscribe(self, cursor: int | None, buffer: int) -> EventStream:
        stream = EventStream(cursor, buffer, self._open.discard)
        if self._ended is not None:  # too late for anything: it ends as the others did
            stream.end(self._ended[0])
        else:
            self._open.add(stream)
        return stream

    def emit(self, event: Event) -> None:
        for stream in tuple(self._open):
            stream.offer(event)

    def end(self, failure: Exception | None = None) -> None:
        """End every stream (with `failure`, if any) and forget them. Streams subscribed later
        end the same way; a stop after a failure does not turn it into a clean end."""
        if self._ended is None:
            self._ended = (failure,)
        for stream in tuple(self._open):
            stream.end(self._ended[0])
        self._open.clear()


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
    the questions open, the event streams open, and what the driver died of."""

    units: int
    queued: bool
    open_questions: int
    subscribers: int
    failure: Exception | None


class Driver:
    """Runs one session's cursors over providers, tools, and extensions.

    `providers` maps each provider name to the provider a model with that `provider` is
    sampled by; each must be named by its own `name`. The program that built them closes
    them. `catalog` is where a `Config` node's model name is resolved, at each sample.

    Frontend calls (`input`, `send`, `step`, `go_to`, `set_auto`, `abort`, `open`, `add_item`,
    `set_label`, `remove_label`, `respond`) run on the loop thread and then poke the pump;
    each says `Saved` only if something a frontend could see changed. Once the driver has
    failed or stopped they raise (the failure, or `RuntimeError`), as `rest` does; an event
    stream subscribed then ends at once. Extensions are listed in the constructor, in
    hook order, and checked there like tools (`ExtensionContractError`); `model.md` does
    not yet say how `config_at` names them.
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
        self._streams = _Streams()
        self._catalog = catalog
        self._tools: Mapping[str, Tool]
        self._tools, self._progress_tools = check_tools(tools)
        self._extensions = check_extensions(extensions)
        self._queue: asyncio.Queue[tuple[int, Answer] | None] = asyncio.Queue()
        self._out: dict[int, _Outstanding] = {}
        self._questions = _Questions()
        self._tasks = _Tasks()
        self._poke = partial(self._queue.put_nowait, None)  # wake the pump
        self._failure: Exception | None = None
        self._stopped = False
        self._rested = _Waiters()

    def events(self, cursor: int | None = None, *, buffer: int = 256) -> EventStream:
        """Subscribe, now, to the events of `cursor` (default: every cursor): each `Live`
        delta the providers stream and one `Saved` after each answer applied to core. The
        stream has its own `buffer` of events; a full buffer drops `Live` events, never
        `Saved`. Subscribers never slow the pump. `aclose()` unsubscribes."""
        return self._streams.subscribe(cursor, buffer)

    def _fail(self, failure: Exception) -> None:
        """Record what the pump or an effect died of, cancel the other effects (they would run
        on, billing, until `stop`), and end every event stream with it. The first failure is
        the root cause: a later one changes nothing."""
        if self._failure is not None:
            return
        self._failure = failure
        current = asyncio.current_task()
        for effect in tuple(self._tasks.effects):
            if effect is not current:
                effect.cancel()
        self._streams.end(failure)

    def use_catalog(self, catalog: ModelCatalog) -> None:
        """Resolve model names in `catalog` from the next sample dispatched on."""
        self._catalog = catalog

    # Lifecycle

    def start(self) -> None:
        """Start the pump. Call inside the running loop."""
        self._tasks.spawn(self._pump())

    async def stop(self) -> None:
        """Cancel everything without answering, as a crash would. `rest` and every frontend
        call raise from now on; event streams end, later ones at once."""
        self._stopped = True
        tasks = list(self._tasks.all)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._streams.end()
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
            subscribers=len(self._streams),
            failure=self._failure,
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

    def questions(self, cursor: int) -> list[Question]:
        """The cursor's open questions, oldest first."""
        return self._questions.open(cursor)

    # Frontend calls

    @contextmanager
    def _frontend(self, cursor: int) -> Generator[None]:
        """A frontend call that changes `cursor`: say `Saved` if what a frontend could see
        changed, and wake the pump either way. Raises once the driver has failed or stopped:
        nothing is left to run what the call would start."""
        _running(self._failure, self._stopped)
        before = _seen(self.session, cursor)
        yield
        if _seen(self.session, cursor) != before:
            self._streams.emit(Saved(cursor))
        self._poke()

    def input(self, cursor: int, client: str, text: str) -> int:
        with trace.span("driver", "input"), self._frontend(cursor):
            return self.session.input(cursor, client, text)

    def send(self, cursor: int, client: str, text: str) -> int:
        """Input with auto-step on."""
        with trace.span("driver", "send"), self._frontend(cursor):
            item = self.session.input(cursor, client, text)
            self.session.set_auto(cursor, True)
            return item

    def step(self, cursor: int) -> None:
        with self._frontend(cursor):
            self.session.step(cursor)

    def go_to(self, cursor: int, to: int, client: str) -> bool:
        """Move, aborting a busy step first (which declines its open questions). True if
        core had an effect in flight to cancel."""
        with self._frontend(cursor):
            cancel = self.session.go_to(cursor, to, client, self.clock.now())
            if cancel:
                _cancel(self._out, cursor)
            self._questions.decline(cursor)
        return cancel

    def set_auto(self, cursor: int, on: bool) -> None:
        with self._frontend(cursor):
            self.session.set_auto(cursor, on)

    def open(self, at: int, client: str) -> int:
        """Open a cursor at `at` for `client`; it wakes."""
        _running(self._failure, self._stopped)
        cursor = self.session.open(at, Author.Frontend(client), self.clock.now())
        self._streams.emit(Saved(cursor))
        self._poke()
        return cursor

    def add_item(self, to: int, data: ItemData) -> int:
        """Add an item to a cursor's scratchpad; the cursor wakes. For whoever has something
        to tell a resting cursor from outside a hook: a timer extension, `tau serve` relaying a
        webhook. An `ItemData.Input` is what `input` and `send` add (they say who typed it), so
        it is refused here. A hook adds its extension's item with its handle's `add_item`."""
        if isinstance(data, ItemData.Input):
            raise TypeError("an input item is added with `input` or `send`, not `add_item`")
        with self._frontend(to):
            return self.session.add_item(to, data)

    def set_label(self, label: str, cursor: int) -> None:
        """Put `label` on `cursor`; a label set elsewhere moves (both cursors are `Saved`)."""
        with self._frontend(cursor):
            holder = dict(self.session.labels()).get(label)
            self.session.set_label(label, cursor)
        if holder is not None and holder != cursor:
            self._streams.emit(Saved(holder))

    def remove_label(self, label: str) -> None:
        """Remove `label`; raises `NoSuchLabel`."""
        with self._frontend(self.session.label(label)):
            self.session.remove_label(label)

    def abort(self, cursor: int) -> bool:
        """Abort the cursor's step and decline its open questions. True if core had
        effects in flight to cancel."""
        with self._frontend(cursor):
            cancel = self.session.abort(cursor)
            if cancel:
                _cancel(self._out, cursor)
            self._questions.decline(cursor)
        return cancel

    def respond(self, cursor: int, response: str | None) -> None:
        """Respond to the cursor's oldest open question; `None` declines it."""
        self._questions.respond(cursor, response)

    # The pump

    async def _pump(self) -> None:
        try:
            while self._failure is None:
                for cursor in self.session.cursors():
                    if cursor not in self._out and self.session.cursor(cursor).busy:
                        with trace.span("driver", "next_job"):
                            job = self.session.next_job(cursor)
                        self._dispatch(cursor, job)
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
        before = len(self.session.tree)
        seen = _seen(self.session, cursor)
        with trace.span("driver", "answer"):
            self.session.answer(cursor, answer, self.clock.now())
        # An answer that changed nothing a frontend sees (a call answered before its batch is
        # whole, a hook that wrote nothing) has nothing to announce.
        reply = isinstance(answer, Answer.Sampled)
        if reply or _seen(self.session, cursor) != seen:
            self._streams.emit(Saved(cursor, _answered(self.session, before), reply))
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
        live = partial(_live, self._streams, cursor)
        announce = _announcer(self._streams, cursor)
        match job:
            case Job.Rest():
                return
            case Job.Hooks():
                at = None if job.node is None else session.tree.node(job.node)
                subject = Subject(draft=job.draft, node=at, ending=job.ending)

                def cursor_for(ext: Extension) -> Cursor:
                    return Cursor(
                        session,
                        cursor,
                        ext.name,
                        self._questions.ask,
                        self.clock.now,
                        self._poke,
                        announce,
                    )

                failed = partial(_hook_error, self._streams, cursor)
                effect = partial(run_hooks, job, subject, self._extensions, cursor_for, failed)
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
                runs = [
                    CallRun(
                        c,
                        lambda ext: NodeHandle(
                            session, node, ext.name, self.clock.now, self._poke, announce
                        ),
                        self._tools,
                        self._extensions,
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


def _cancel(outs: dict[int, _Outstanding], cursor: int) -> None:
    """Core said abort cancels the effect out for `cursor`."""
    out = outs.get(cursor)
    if out is None:
        raise RuntimeError(f"cursor {cursor}: abort cancels an effect, but none is out")
    out.cancel()


def _seen(session: Session, cursor: int) -> tuple[object, ...]:
    """What a frontend could see change: the tree's size, the labels, and the cursor's
    place, state, labels and items."""
    state = session.cursor(cursor)
    return (
        len(session.tree),
        session.labels(),
        state.at,
        state.auto,
        state.busy,
        state.labels,
        state.items,
    )


def _answered(session: Session, since: int) -> tuple[CallKey, ...]:
    """The calls answered by the `Results` nodes added since node count `since`, each as
    `(node of its batch, call id)`."""
    nodes = (session.tree.node(i) for i in range(since, len(session.tree)))
    return tuple(
        (cast("int", node.parent), call)
        for node in nodes
        if isinstance(body := node.body, Body.Results)
        for call, _, _ in body.outcomes
    )


def _live(streams: _Streams, cursor: int, delta: LiveDelta) -> None:
    """A provider's delta or a tool's progress, as the `Live` of the cursor it serves."""
    streams.emit(Live(cursor, delta))


def _hook_error(streams: _Streams, cursor: int, point: str, extension: str, error: str) -> None:
    """A cursor hook's failure, as the `HookError` of the cursor it ran for."""
    streams.emit(HookError(cursor, point, extension, error))


def _announcer(streams: _Streams, own: int) -> Changed:
    """The `changed` of a handle working for `own`: a `Saved` for the cursor it changed."""
    return lambda changed: streams.emit(Saved(own if changed is None else changed))

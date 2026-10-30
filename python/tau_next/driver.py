"""The driver: performs each cursor's jobs and answers the results back.

One pump coroutine per session is the only caller of `next_job` and `answer`
(D11). Core decides every step: which job comes next, what a sample's end
records, what a batch's join is, when a go-to applies. Python owns only the
rules about performing one effect:

- Hooks run in extension order; the first string a hook returns halts the
  rest, and a hook that raises fails the job (a cursor hook) or makes the call
  `Err("hook error")` (a tool hook).
- A call's `pre_tool` hooks may answer it before the tool runs; a call that
  names no tool is `Err("unknown tool: ...")`.
- An interrupted call skips `pre_result`.
- A question open when its cursor is aborted or sent elsewhere is declined.
"""

import asyncio
from collections.abc import Awaitable, Callable, Coroutine, Mapping, Sequence
from dataclasses import dataclass, field
from functools import partial

from tau_next import trace
from tau_next._core import Answer, Author, ItemData, Job, Session
from tau_next.clock import SystemClock
from tau_next.config_layers import ModelCatalog
from tau_next.effects import CallRun, SampleRun, run_hooks
from tau_next.handles import Cursor, NodeHandle, Subject
from tau_next.protocols import Clock, Extension, Provider, Tool, check_tools


@dataclass
class Question:
    """An open question: who it is for, what it asks, and the future its response fills."""

    target: str
    text: str
    future: asyncio.Future[str | None]


@dataclass
class Outstanding:
    """One cursor's effect in flight: the job it answers, and how to cancel it."""

    units: list[asyncio.Task[None]]
    sample: SampleRun | None = None
    calls: list[CallRun] = field(default_factory=lambda: [])


class Driver:
    """Runs one session's cursors over a provider, tools, and extensions.

    `catalog` is where a `Config` node's model name is resolved, at each sample.

    Frontend calls (`input`, `send`, `step`, `go_to`, `set_auto`, `abort`,
    `open`, `respond`) run on the loop thread and then poke the pump.
    Extensions are listed in the constructor, in hook order; `model.md` does
    not yet say how `config_at` names them.
    """

    def __init__(  # noqa: PLR0913 - the driver is given each thing it drives or asks
        self,
        session: Session,
        provider: Provider,
        catalog: ModelCatalog,
        *,
        tools: Sequence[Tool] = (),
        extensions: Sequence[Extension] = (),
        clock: Clock | None = None,
    ) -> None:
        self.session = session
        self.clock: Clock = clock if clock is not None else SystemClock()
        self._provider = provider
        self._catalog = catalog
        self._tools: Mapping[str, Tool] = check_tools(tools)
        self._extensions = tuple(extensions)
        self._queue: asyncio.Queue[tuple[int, Answer] | None] = asyncio.Queue()
        self._out: dict[int, Outstanding] = {}
        self._questions: dict[int, list[Question]] = {}
        self._tasks: set[asyncio.Task[None]] = set()
        self._units = 0
        self._failure: Exception | None = None
        self._watchers: list[asyncio.Future[None]] = []

    def use_catalog(self, catalog: ModelCatalog) -> None:
        """Resolve model names in `catalog` from the next sample dispatched on."""
        self._catalog = catalog

    # Lifecycle

    def start(self) -> None:
        """Start the pump. Call inside the running loop."""
        self._spawn(self._pump())

    async def stop(self) -> None:
        """Cancel everything without answering, as a crash would."""
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._out.clear()
        self._questions.clear()
        self._queue = asyncio.Queue()

    # What a harness needs to tell quiet from busy

    @property
    def units(self) -> int:
        """Effects running now."""
        return self._units

    @property
    def open_questions(self) -> int:
        return sum(len(q) for q in self._questions.values())

    def queued(self) -> bool:
        """The pump has something to do: a report or poke to take, or a busy cursor
        with no effect out."""
        return not self._queue.empty() or any(
            self.session.cursor(c).busy and c not in self._out for c in self.session.cursors()
        )

    def check(self) -> None:
        """Raise the first exception an effect or the pump died of."""
        if self._failure is not None:
            raise self._failure

    async def rest(self, cursor: int) -> None:
        """Wait until the cursor is at rest with no effect out. Raises what an effect or the
        pump died of. A cursor held by an open question or a gate does not rest."""
        while True:
            self.check()
            if not self._active(cursor):
                return
            watcher = asyncio.get_running_loop().create_future()
            self._watchers.append(watcher)
            await watcher

    def _active(self, cursor: int) -> bool:
        return self.session.cursor(cursor).busy or cursor in self._out

    def _notify(self) -> None:
        watchers, self._watchers = self._watchers, []
        for watcher in watchers:
            if not watcher.done():
                watcher.set_result(None)

    def questions(self, cursor: int) -> list[Question]:
        return list(self._questions.get(cursor, []))

    # Frontend calls

    def input(self, cursor: int, client: str, text: str) -> int:
        with trace.span("driver", "input"):
            item = self.session.input(cursor, client, text)
        self._poke()
        return item

    def send(self, cursor: int, client: str, text: str) -> int:
        """Input with auto-step on."""
        with trace.span("driver", "send"):
            item = self.session.input(cursor, client, text)
            self.session.set_auto(cursor, True)
        self._poke()
        return item

    def step(self, cursor: int) -> None:
        self.session.step(cursor)
        self._poke()

    def go_to(self, cursor: int, to: int, client: str) -> bool:
        """Move, aborting a busy step first (which declines its open questions). True if
        core had an effect in flight to cancel."""
        cancel = self.session.go_to(cursor, to, client, self.clock.now())
        if cancel:
            self._cancel(cursor)
        self._decline(cursor)
        self._poke()
        return cancel

    def set_auto(self, cursor: int, on: bool) -> None:
        self.session.set_auto(cursor, on)
        self._poke()

    def open(self, at: int, by: Author) -> int:
        cursor = self.session.open(at, by, self.clock.now())
        self._poke()
        return cursor

    def add_item(self, to: int, data: ItemData) -> int:
        """Add an item to a cursor's scratchpad (an extension's call): wakes it."""
        item = self.session.add_item(to, data)
        self._poke()
        return item

    def abort(self, cursor: int) -> bool:
        """Abort the cursor's step and decline its open questions. True if core had
        effects in flight to cancel."""
        cancel = self.session.abort(cursor)
        if cancel:
            self._cancel(cursor)
        self._decline(cursor)
        self._poke()
        return cancel

    def respond(self, cursor: int, response: str | None) -> None:
        """Respond to the cursor's oldest open question; `None` declines it."""
        open_ = self._questions.get(cursor)
        if not open_:
            raise LookupError(f"cursor {cursor} has no open question")
        open_.pop(0).future.set_result(response)

    async def ask(self, cursor: int, target: str, text: str) -> str | None:
        """`cursor.ask`: hold the question until it is responded to or declined. Core
        knows nothing of it: the hook awaiting it keeps its job out, so the cursor is busy."""
        question = Question(target, text, asyncio.get_running_loop().create_future())
        self._questions.setdefault(cursor, []).append(question)
        return await question.future

    def _decline(self, cursor: int) -> None:
        for question in self._questions.pop(cursor, []):
            question.future.set_result(None)

    # The pump

    def _poke(self) -> None:
        self._queue.put_nowait(None)

    def _spawn(self, coro: Coroutine[object, object, None]) -> asyncio.Task[None]:
        task = asyncio.ensure_future(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    async def _pump(self) -> None:
        try:
            while self._failure is None:
                self._dispatch_ready()
                self._notify()
                item = await self._queue.get()
                if item is not None:
                    self._deliver(*item)
        except Exception as e:
            self._failure = e
        finally:
            self._notify()

    def _dispatch_ready(self) -> None:
        for cursor in self.session.cursors():
            if cursor not in self._out and self.session.cursor(cursor).busy:
                with trace.span("driver", "next_job"):
                    job = self.session.next_job(cursor)
                self._dispatch(cursor, job)

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

    def _unit(self, cursor: int, effect: Callable[[], Awaitable[Answer]]) -> asyncio.Task[None]:
        """Run an effect as a task that reports `(cursor, Answer)` to the pump."""

        async def unit() -> None:
            try:
                self._queue.put_nowait((cursor, await effect()))
            except Exception as e:
                self._failure = self._failure or e
                self._poke()

        self._units += 1
        task = self._spawn(unit())
        task.add_done_callback(self._unit_done)
        return task

    def _unit_done(self, _: asyncio.Task[None]) -> None:
        self._units -= 1

    def _dispatch(self, cursor: int, job: Job) -> None:
        session = self.session
        match job:
            case Job.Rest():
                return
            case Job.Hooks():
                at = None if job.node is None else session.tree.node(job.node)
                subject = Subject(draft=job.draft, node=at, ending=job.ending)
                effect = partial(
                    run_hooks, job, subject, self._extensions, self._cursor_for(cursor)
                )
                self._out[cursor] = Outstanding([self._unit(cursor, effect)])
            case Job.Sample():
                sample = SampleRun(
                    self._provider, self._catalog, session, cursor, tuple(self._tools.values())
                )
                self._out[cursor] = Outstanding([self._unit(cursor, sample.run)], sample=sample)
            case Job.RunCalls(node=node, calls=calls):
                if not calls:
                    raise RuntimeError(f"cursor {cursor}: a batch with no calls at node {node}")
                runs = [
                    CallRun(
                        c,
                        lambda ext: NodeHandle(session, node, ext.name, self.clock.now, self._poke),
                        self._tools,
                        self._extensions,
                    )
                    for c in calls
                ]
                units = [self._unit(cursor, r.run) for r in runs]
                self._out[cursor] = Outstanding(units, calls=runs)
            case _:
                raise RuntimeError(f"cursor {cursor}: a job this driver does not know: {job}")

    def _cursor_for(self, cursor: int) -> Callable[[Extension], Cursor]:
        return lambda ext: Cursor(
            self.session, cursor, ext.name, self.ask, self.clock.now, self._poke
        )

    def _cancel(self, cursor: int) -> None:
        """Core said abort cancels the effect: stop its tasks. Each call unit and the
        sample answer with what they have."""
        out = self._out.get(cursor)
        if out is None:
            raise RuntimeError(f"cursor {cursor}: abort cancels an effect, but none is out")
        if out.sample is not None:
            out.sample.cancel()
        for call in out.calls:
            call.abort()

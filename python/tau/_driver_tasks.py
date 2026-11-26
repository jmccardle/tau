"""The driver's own bookkeeping of tasks and waits: who waits for the pump to rest, which
cursors' waits have ended, every task the driver runs, and calls made safe from another thread.
Plumbing of `Driver`, apart so it stays one concept a file."""

import asyncio
import concurrent.futures
import contextvars
import threading
from collections import Counter
from collections.abc import Callable, Coroutine, Generator
from contextlib import contextmanager


class Waiters:
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


class Leavers:
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


EFFECT: contextvars.ContextVar[asyncio.Task[None] | None] = contextvars.ContextVar(
    "effect", default=None
)
"""The effect task the running code belongs to, tasks it starts included."""


class Tasks:
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
                EFFECT.set(task)
                await coro

            run = run_as_effect()
        task = asyncio.create_task(run)
        if effect:  # a task cancelled before its first step never started `coro`: close it
            task.add_done_callback(lambda _: coro.close())
        for held in (self.all, self.effects) if effect else (self.all,):
            held.add(task)
            task.add_done_callback(held.discard)


def on_loop(
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


def on_loop_wait[T](
    home: Callable[[], tuple[asyncio.AbstractEventLoop, int] | None], call: Callable[[], T]
) -> T:
    """`call()` on the loop's thread, whichever thread asks, and its result (or what it
    raises) returned to the asker, who waits for it. Unlike `on_loop` this is for a call whose
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

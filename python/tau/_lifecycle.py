"""Extensions' start and stop (D32 section 4): the lifecycle handles, the tasks an extension
`spawn`s, and the timeouts.

An extension's `start(tree)` runs once, in load order, before the driver takes a job (and when a
reload adds it); `tree` stays live until its `stop` has returned. Its `stop()` runs at shutdown
(and when a reload removes it) in reverse load order, after the tasks the extension spawned are
cancelled and awaited. A `start` that raises or outlives its timeout stops the start: the ones
already started are stopped in reverse, and `StartFailed` (an `ExtensionLoadError`) names it. A
`stop` that raises or outlives its timeout is written to stderr with the extension's name (and
told to clients as an `error` `Notice`, when they can be there), and the next one runs:
shutdown must finish.
"""

import asyncio
import sys
import threading
import traceback
from collections.abc import Callable, Coroutine, Mapping, Sequence
from typing import Any, Final, cast

from tau._events import Notice
from tau._extension_error import ExtensionLoadError
from tau._extensions import ExtensionSource
from tau._handles import TreeHandle
from tau._protocols import Extension

START_TIMEOUT: Final = 30.0
"""Seconds an extension's `start` may take before the start fails, unless the config files set
`extensions.NAME.start_timeout`."""
STOP_TIMEOUT: Final = 5.0
"""Seconds an extension's `stop`, with the cancelling of its spawned tasks, may take before it is
reported and the next one runs, unless the config files set `extensions.NAME.stop_timeout`."""

_Start = Callable[[TreeHandle], Coroutine[Any, Any, object]]

Timeouts = Callable[[str], tuple[float | None, float | None]]
"""The configured `(start, stop)` timeouts of an extension, `None` for the default."""


class StartFailed(ExtensionLoadError):
    """An extension's `start` raised or did not return in time: `extension` is its name. The
    message is `extension 'NAME' did not start: Type: msg`."""

    def __init__(self, extension: str, why: str) -> None:
        super().__init__(f"extension {extension!r} did not start: {why}")
        self.extension = extension


def _why(error: BaseException, seconds: float) -> str:
    """`"Type: msg"` of a failed `start` or `stop`; a timeout says how long it was given."""
    if isinstance(error, TimeoutError):
        return f"TimeoutError: it did not return within {seconds:g} s"
    return f"{type(error).__name__}: {error}"


class Lifecycle:
    """The extensions started now, each with its lifecycle handle, and the tasks each spawned.
    `handle(name)` makes a lifecycle handle; `timeouts(name)` reads the configured timeouts;
    `notify` tells every client a `Notice` (an empty hub drops it); `check` raises once the driver
    has failed or stopped."""

    def __init__(
        self,
        handle: Callable[[str], TreeHandle],
        timeouts: Timeouts,
        notify: Callable[[Notice], None],
        check: Callable[[], None],
    ) -> None:
        self._handle = handle
        self._timeouts = timeouts
        self._notify = notify
        self._check = check
        self._started: list[tuple[Extension, TreeHandle | None]] = []  # load order
        self._spawned: dict[str, set[asyncio.Task[None]]] = {}
        self._starting: asyncio.Task[None] | None = None
        self.held = False  # the driver takes no job while a start or reload runs
        self._thread: int | None = None

    def starting(self) -> bool:
        """Whether the running code is an extension's `start` (its own task, not one it made)."""
        try:
            current = asyncio.current_task()
        except RuntimeError:  # no loop in this thread: a worker thread is never a start
            return False
        return current is not None and current is self._starting

    def started(self) -> tuple[Extension, ...]:
        """The extensions started now, in load order."""
        return tuple(e for e, _ in self._started)

    # Tasks

    def spawn(self, extension: str, coro: Coroutine[Any, Any, object]) -> "asyncio.Task[None]":
        """`TreeHandle.spawn`: run `coro` as `extension`'s task; a failure in it is reported."""
        try:
            self._check()
            loop = asyncio.get_running_loop()  # RuntimeError off the loop's thread
            if self._thread is not None and threading.get_ident() != self._thread:
                raise RuntimeError("spawn: call it on the event loop's thread")
        except BaseException:
            coro.close()
            raise
        task = loop.create_task(self._watched(extension, coro))
        held = self._spawned.setdefault(extension, set())
        held.add(task)
        task.add_done_callback(held.discard)
        return task

    async def _watched(self, extension: str, coro: Coroutine[Any, Any, object]) -> None:
        try:
            await coro
        except Exception as e:  # its own failure, reported; a cancel passes
            print(f"tau: a task of extension {extension!r} failed:", file=sys.stderr)
            traceback.print_exception(e, file=sys.stderr)
            why = f"task failed: {type(e).__name__}: {e}"
            self._notify(Notice(why, None, extension, "error"))

    async def _cancel_tasks(self, extension: str) -> None:
        tasks = self._spawned.pop(extension, set())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    # Start and stop

    async def start(self, extensions: Sequence[Extension]) -> None:
        """Start each of `extensions` that is not started, in order, each as its own task within
        its timeout. On a failure the ones this call started are stopped in reverse, and
        `StartFailed` names the extension."""
        self._thread = threading.get_ident()
        begun: list[Extension] = []
        for ext in extensions:
            if any(ext is e for e, _ in self._started):
                continue
            start = cast("_Start | None", getattr(ext, "start", None))
            handle = None if start is None else self._handle(ext.name)
            if start is not None and handle is not None:
                seconds = self._timeouts(ext.name)[0] or START_TIMEOUT
                try:
                    await self._run_start(start, handle, seconds)
                except Exception as e:
                    self._started.append((ext, handle))  # its tasks and handle go with the rest
                    begun.append(ext)
                    await self.stop(begun, skip_stop=ext)
                    raise StartFailed(ext.name, _why(e, seconds)) from e
            self._started.append((ext, handle))
            begun.append(ext)

    async def _run_start(self, start: "_Start", handle: TreeHandle, seconds: float) -> None:
        """`start(handle)` as its own task (what `starting` knows it by), within `seconds`."""

        async def run() -> None:
            # Set inside the task: an eager task factory runs its first step before
            # `create_task` returns.
            self._starting = asyncio.current_task()
            await start(handle)

        task = asyncio.get_running_loop().create_task(run())
        try:
            async with asyncio.timeout(seconds):
                await task
        except TimeoutError:  # the timeout cancelled it: wait for it to be over
            await asyncio.gather(task, return_exceptions=True)
            raise
        finally:
            self._starting = None

    async def stop(
        self, extensions: Sequence[Extension], *, skip_stop: Extension | None = None
    ) -> None:
        """Stop each of `extensions` that is started, in reverse load order: cancel and await its
        spawned tasks, run its `stop` (not for `skip_stop`, whose `start` failed) within its
        timeout, and retire its lifecycle handle. A failure is reported, never raised."""
        leaving = [(e, h) for e, h in self._started if any(e is x for x in extensions)]
        for ext, handle in reversed(leaving):
            stop = None if ext is skip_stop else getattr(ext, "stop", None)
            seconds = self._timeouts(ext.name)[1] or STOP_TIMEOUT
            try:
                async with asyncio.timeout(seconds):
                    await self._cancel_tasks(ext.name)
                    if stop is not None:
                        await stop()
            except Exception as e:
                self._report(ext.name, e, seconds)
            finally:
                if handle is not None:
                    handle.retire()
                self._started = [(x, h) for x, h in self._started if x is not ext]

    def _report(self, extension: str, error: BaseException, seconds: float) -> None:
        print(f"tau: extension {extension!r} did not stop cleanly:", file=sys.stderr)
        traceback.print_exception(error, file=sys.stderr)
        why = _why(error, seconds)
        self._notify(Notice(f"did not stop cleanly: {why}", None, extension, "error"))


def configured_timeouts(timeouts: Mapping[str, tuple[float | None, float | None]]) -> Timeouts:
    """`Timeouts` reading `timeouts` (`ModelCatalog.timeouts`)."""
    return lambda name: timeouts.get(name, (None, None))


def start_failure(
    extension: str, why: str, sources: Sequence[ExtensionSource]
) -> ExtensionLoadError:
    """The load failure a `start` that failed is (D19's shape): what failed, the source that
    provides the extension, the error, and the command that starts without it. An extension no
    source provides (the program's own) is named alone."""
    found = next((s for s in sources if extension in s.extensions), None)
    if found is None:
        return ExtensionLoadError(f"{why}; tau did not start.")
    if found.kind == "explicit":
        tell = f"fix it or drop the flag: {found.origin}"
    else:
        tell = f"to start without it: tau ext disable {found.key}"
        if found.kind in ("package", "user", "project"):
            tell += "   (or: tau --no-extensions)"
    lines = [f'extension "{found.key}" failed to start; tau did not start.']
    lines += [f"  source: {found.origin}", f"  {why}", f"  {tell}"]
    return ExtensionLoadError("\n".join(lines), [found])

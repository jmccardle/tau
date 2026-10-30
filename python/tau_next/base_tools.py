"""The base tools: `read`, `write`, `grep`, `bash`, implemented in Rust (`crates/tau-tools`).

Each class satisfies `Tool`. The work runs on a worker thread with the GIL released, so
calls run in parallel, even on free-threaded builds, and never block the event loop.

    tools = base_tools(cwd="/work/project")
    driver = Driver(session, providers, catalog, tools=tools)

`read`, `grep` and `bash` limit their output to `max_output` bytes (default 10_000): past
it, the model gets the kept lines and a note naming the omitted line count and a file in
`<temp dir>/tau-<euid>/` that holds the full output (up to `max_overflow` bytes, default
64 MiB). `read` and `grep` keep the head, `bash` the tail. `write` answers one short line,
so the limit does not apply to it.

An expected failure (bad arguments, a missing file, a command that exits nonzero, an overflow
file that cannot be written) answers `Outcome.Err` with a message for the model; nothing is
raised out of `call`. A bad constructor argument raises `ValueError` or `NotADirectoryError`.

Cancelling `call` stops the work: `read` and `grep` stop at their next check, `bash` terminates
its process group (SIGTERM, then SIGKILL after `kill_grace_ms`) and waits for it to end.
"""

import asyncio
import json
import os
import threading
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Protocol

from tau_next import _core
from tau_next._core import Outcome

DEFAULT_MAX_OUTPUT = 10_000
DEFAULT_MAX_OVERFLOW = 64 * 1024 * 1024
DEFAULT_BASH_TIMEOUT_MS = 30_000
DEFAULT_BASH_MAX_TIMEOUT_MS = 600_000
DEFAULT_KILL_GRACE_MS = 2_000


class _Engine(Protocol):
    @property
    def name(self) -> str: ...
    @property
    def description(self) -> str: ...
    @property
    def parameters_json(self) -> str: ...


def _working_directory(cwd: str | os.PathLike[str] | None) -> Path:
    path = Path.cwd() if cwd is None else Path(cwd).absolute()
    if not path.is_dir():
        raise NotADirectoryError(f"the working directory does not exist: {path}")
    return path


class _Described:
    """name, description and parameters of a Rust engine."""

    def __init__(self, engine: _Engine) -> None:
        self._described = engine
        self._parameters: dict[str, object] = json.loads(engine.parameters_json)

    @property
    def name(self) -> str:
        return self._described.name

    @property
    def description(self) -> str:
        return self._described.description

    @property
    def parameters(self) -> Mapping[str, object]:
        return self._parameters


async def _in_own_thread[T](work: Callable[[], T]) -> T:
    """Run `work` on a thread of its own (not the loop's default pool, which a few
    long-running calls could fill) and wait for it. Cancelling the wait does not stop the thread."""
    loop = asyncio.get_running_loop()
    done: asyncio.Future[T] = loop.create_future()

    def deliver(result: T | None, error: BaseException | None) -> None:
        if done.cancelled():
            return
        if error is not None:
            done.set_exception(error)
        else:
            done.set_result(result)  # pyright: ignore[reportArgumentType]

    def run() -> None:
        try:
            result = work()
        except BaseException as e:
            loop.call_soon_threadsafe(deliver, None, e)
        else:
            loop.call_soon_threadsafe(deliver, result, None)

    threading.Thread(target=run, name="tau-tool", daemon=True).start()
    return await done


class _Cancellable(_Described):
    """A tool whose Rust engine runs to completion in one blocking call, checking a
    `Cancel` flag as it goes."""

    def __init__(self, engine: "_core.Read | _core.Grep") -> None:
        super().__init__(engine)
        self._engine = engine

    async def call(self, args_raw: str) -> Outcome:
        cancel = _core.Cancel()
        work = asyncio.ensure_future(
            asyncio.to_thread(self._engine.run, args_raw, cancel)  # pyright: ignore[reportArgumentType]
        )
        try:
            return await asyncio.shield(work)
        except asyncio.CancelledError:
            cancel.cancel()
            await asyncio.wait([work])  # stops at its next check
            raise


class ReadTool(_Cancellable):
    """`read`: a text file, from line `offset` for `limit` lines. Keeps the head of long output."""

    def __init__(
        self,
        cwd: str | os.PathLike[str] | None = None,
        *,
        max_output: int = DEFAULT_MAX_OUTPUT,
        overflow_dir: str | os.PathLike[str] | None = None,
        max_overflow: int = DEFAULT_MAX_OVERFLOW,
    ) -> None:
        engine = _core.Read(_working_directory(cwd), max_output, overflow_dir, max_overflow)
        super().__init__(engine)


class GrepTool(_Cancellable):
    """`grep`: a regex over a file or a directory tree, honoring .gitignore. Keeps the head."""

    def __init__(
        self,
        cwd: str | os.PathLike[str] | None = None,
        *,
        max_output: int = DEFAULT_MAX_OUTPUT,
        overflow_dir: str | os.PathLike[str] | None = None,
        max_overflow: int = DEFAULT_MAX_OVERFLOW,
    ) -> None:
        engine = _core.Grep(_working_directory(cwd), max_output, overflow_dir, max_overflow)
        super().__init__(engine)


class WriteTool(_Described):
    """`write`: a whole file, atomically, creating parent directories."""

    def __init__(self, cwd: str | os.PathLike[str] | None = None) -> None:
        engine = _core.Write(_working_directory(cwd))
        super().__init__(engine)
        self._engine = engine

    async def call(self, args_raw: str) -> Outcome:
        return await asyncio.to_thread(self._engine.run, args_raw)


class BashTool(_Described):
    """`bash`: a shell command with a timeout. Keeps the tail of long output.

    `timeout_ms` is the timeout of a call that gives none, `max_timeout_ms` the most a call may
    ask for (more is an `Err`), `kill_grace_ms` the time between SIGTERM and SIGKILL.
    Cancelling `call` terminates the command's whole process group and waits for it to end
    before the `CancelledError` propagates: no child is left running. Each call has a thread
    of its own.
    """

    def __init__(  # noqa: PLR0913 - one parameter per setting of the tool
        self,
        cwd: str | os.PathLike[str] | None = None,
        *,
        max_output: int = DEFAULT_MAX_OUTPUT,
        overflow_dir: str | os.PathLike[str] | None = None,
        max_overflow: int = DEFAULT_MAX_OVERFLOW,
        timeout_ms: int = DEFAULT_BASH_TIMEOUT_MS,
        max_timeout_ms: int = DEFAULT_BASH_MAX_TIMEOUT_MS,
        kill_grace_ms: int = DEFAULT_KILL_GRACE_MS,
    ) -> None:
        engine = _core.Bash(
            _working_directory(cwd),
            max_output,
            overflow_dir,
            max_overflow,
            timeout_ms,
            max_timeout_ms,
            kill_grace_ms,
        )
        super().__init__(engine)
        self._engine = engine

    async def call(self, args_raw: str) -> Outcome:
        run = self._engine.start(args_raw)
        waiting = asyncio.ensure_future(_in_own_thread(run.wait))
        try:
            return await asyncio.shield(waiting)
        except asyncio.CancelledError:
            run.kill()
            await asyncio.wait([waiting])  # returns once the group is dead and the pipe closed
            raise


def base_tools(  # noqa: PLR0913 - the settings the four tools share
    cwd: str | os.PathLike[str] | None = None,
    *,
    max_output: int = DEFAULT_MAX_OUTPUT,
    overflow_dir: str | os.PathLike[str] | None = None,
    max_overflow: int = DEFAULT_MAX_OVERFLOW,
    bash_timeout_ms: int = DEFAULT_BASH_TIMEOUT_MS,
    bash_max_timeout_ms: int = DEFAULT_BASH_MAX_TIMEOUT_MS,
    bash_kill_grace_ms: int = DEFAULT_KILL_GRACE_MS,
) -> list[ReadTool | WriteTool | GrepTool | BashTool]:
    """`read`, `write`, `grep` and `bash`, all working in `cwd` (default: the current directory)."""
    return [
        ReadTool(cwd, max_output=max_output, overflow_dir=overflow_dir, max_overflow=max_overflow),
        WriteTool(cwd),
        GrepTool(cwd, max_output=max_output, overflow_dir=overflow_dir, max_overflow=max_overflow),
        BashTool(
            cwd,
            max_output=max_output,
            overflow_dir=overflow_dir,
            max_overflow=max_overflow,
            timeout_ms=bash_timeout_ms,
            max_timeout_ms=bash_max_timeout_ms,
            kill_grace_ms=bash_kill_grace_ms,
        ),
    ]

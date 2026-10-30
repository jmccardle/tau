"""The base tools: `read`, `write`, `grep`, `bash`, implemented in Rust (`crates/tau-tools`).

Each class satisfies `Tool`. The work runs on a worker thread with the GIL released, so
calls run in parallel, even on free-threaded builds, and never block the event loop.

    tools = base_tools(cwd="/work/project")
    driver = Driver(session, providers, catalog, tools=tools)

`grep` and `bash` limit their output to `max_output` bytes (default 10_000): past it, the model
gets the kept lines and a note naming the omitted line count and a file that holds the full
output (up to `max_overflow` bytes, default 64 MiB). `grep` keeps the head, `bash` the tail. The
files live in `overflow_dir`, by default `$XDG_RUNTIME_DIR/tau` when that is set, else
`<temp dir>/tau-<euid>`; the directory must be private to the user (a symlink, a directory of
another user or one open to others is refused when a file is first needed, and the answer says
the full output was not saved), and files in it older than a day are removed when it is opened.
`read` limits its output to `max_output` bytes too, but writes no file: it stops after the last
whole line that fits and says which `offset` continues. `write` answers one short line, so the
limit does not apply to it.

An expected failure (bad arguments, a missing file, a command that exits nonzero, an overflow
file that cannot be written) answers `Outcome.Err` with a message for the model; nothing is
raised out of `call`. A bad constructor argument raises `ValueError` or `NotADirectoryError`.

`bash` also takes a `progress` callback (see `Tool`) and passes it the output as it arrives.

Cancelling `call` stops the work: `read` and `grep` stop at their next check, `bash` terminates
its process group (SIGTERM, then SIGKILL after `kill_grace_ms`) and waits for it to end.

A program that uses these tools must turn SIGTERM and SIGHUP (a closed terminal) into that
cancellation, as `asyncio.run` already does for SIGINT: a Python process that dies from either
leaves a running `bash` command orphaned in its own session, and its timeout never fires.
`examples/common.py` (`run_until_signalled`) shows how: `loop.add_signal_handler` for both
signals, cancelling the main task.

`bash` needs Linux (it finds a command's processes by session in `/proc`); building it
anywhere else raises `OSError`.
"""

import asyncio
import contextlib
import json
import os
import sys
import threading
from collections.abc import Callable, Mapping
from functools import partial
from pathlib import Path
from typing import Protocol

from tau_next import _core
from tau_next._core import Outcome
from tau_next.protocols import Progress

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


def _in_own_thread[T](work: Callable[[], T]) -> asyncio.Future[T]:
    """Run `work` on a thread of its own (not the loop's default pool, which a few
    long-running calls could fill); the future resolves with its result. It is a plain future,
    not a task: `asyncio.run` cancels every task on its way out after a second Ctrl-C, and
    cancelling this one would end the wait for a kill still in progress."""
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
    return done


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
    ) -> None:
        engine = _core.Read(_working_directory(cwd), max_output)
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
    before the `CancelledError` propagates, however often it is cancelled meanwhile: no child
    is left running. Each call has a thread
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
        if not sys.platform.startswith("linux"):
            raise OSError(
                f"the bash tool needs Linux (it finds a command's processes in /proc), "
                f"not {sys.platform}"
            )
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

    async def call(self, args_raw: str, *, progress: Progress | None = None) -> Outcome:
        """Run the command. With `progress`, the output also goes to it as it arrives: at
        most one call per 100 ms, carrying all the text since the last, from a thread of the
        engine's own. The answer is the same either way. The command never waits on `progress`:
        a slow one loses the oldest output it has not yet been given (the answer keeps its
        10 kB and the overflow file all of it). The answer does wait for a `progress` call
        already running and for the final flush, one more call, so a slow `progress` delays
        the answer by that much. Calls stop for good when the interpreter begins to exit."""
        run = self._engine.start(args_raw)
        waiting = _in_own_thread(partial(run.wait, progress))
        try:
            return await asyncio.shield(waiting)
        except asyncio.CancelledError:
            run.kill()
            while not waiting.done():  # until the group is dead and the pipe closed
                # cancelled again: the command still has to die before we leave
                with contextlib.suppress(asyncio.CancelledError):
                    await asyncio.wait([waiting])
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
        ReadTool(cwd, max_output=max_output),
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

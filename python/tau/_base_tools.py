"""The base tools: `read`, `write`, `edit`, `grep`, `find`, `bash`, implemented in Rust
(`crates/tau-tools`).

Each class satisfies `Tool`. The work runs on a worker thread with the GIL released, so
calls run in parallel, even on free-threaded builds, and never block the event loop.

A session started by `start_session` (`tau serve`, and later `tau` and `tau -p`) has these tools
from the built-in extension `base_tools`, in the session's working directory: it is on unless the
config says `load: {base_tools: false}` (`tau ext disable base_tools`; `tau ext list` shows it).
`--no-extensions` does not turn it off: that flag skips only the discovered sources. A program
that builds a `Driver` itself gives it the tools:

    tools = base_tools(cwd="/work/project")
    driver = Driver(session, providers, catalog, tools=tools)

`edit`, `grep`, `find` and `bash` limit their output to `max_output` bytes (default 10_000): past
it, the model gets the kept lines and a note naming the omitted line count and a file that holds
the full output (up to `max_overflow` bytes, default 64 MiB). `edit` (its diff), `grep` and
`find` keep the head, `bash` the tail. The files live in `overflow_dir`, by default
`$XDG_RUNTIME_DIR/tau` when that is set, else `<temp dir>/tau-<euid>`; the directory must be
private to the user (a symlink, a directory of another user or one open to others is refused when
a file is first needed, and the answer says the full output was not saved), and files in it older
than a day are removed when it is opened.
`read` limits its output to `max_output` bytes too, but writes no file: it stops after the last
whole line that fits and says which `offset` continues. `write` answers one short line, so the
limit does not apply to it.

`edit` replaces exact text in one file, once (or everywhere with `replace_all`), and answers a
unified diff; the file is written atomically, as `write` does, and keeps its line endings and a
missing final newline. `find` lists the files under a directory whose name matches a glob,
sorted, with `grep`'s walk (ignore rules apply) but listing hidden files and directories as well
(never `.git`).

An expected failure (bad arguments, a missing file, a command that exits nonzero, an overflow
file that cannot be written) answers `Outcome.Err` with a message for the model; nothing is
raised out of `call`. A bad constructor argument raises `ValueError` or `NotADirectoryError`.

`bash` also takes a `progress` callback (see `Tool`) and passes it the output as it arrives.

Cancelling `call` stops the work: `read`, `grep` and `find` stop at their next check, `bash`
terminates its process group (SIGTERM, then SIGKILL after `kill_grace_ms`) and waits for it to
end.

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

from tau import _core
from tau._core import Outcome
from tau._protocols import Progress

DEFAULT_MAX_OUTPUT = 10_000
DEFAULT_MAX_OVERFLOW = 64 * 1024 * 1024
DEFAULT_BASH_TIMEOUT_MS = 30_000
DEFAULT_BASH_MAX_TIMEOUT_MS = 600_000
DEFAULT_KILL_GRACE_MS = 2_000
# `bash` runs on Linux and macOS; on Windows `BashTool` raises and `base_tools` leaves it out.
_BASH_RUNS_HERE = sys.platform in {"linux", "darwin"}


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
        """The tool's name: `read`, `write`, `edit`, `grep`, `find` or `bash`."""
        return self._described.name

    @property
    def description(self) -> str:
        """What the tool does, as the model reads it."""
        return self._described.description

    @property
    def parameters(self) -> Mapping[str, object]:
        """A JSON Schema object describing the tool's arguments."""
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

    def __init__(self, engine: "_core.Read | _core.Grep | _core.Find") -> None:
        super().__init__(engine)
        self._engine = engine

    async def call(self, args_raw: str) -> Outcome:
        """Run the call in `args_raw`, stopping early if the call is cancelled. `Ok` with the
        output, or `Err`."""
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
        """Write the file named in `args_raw`. `Ok` with a short confirmation, or `Err`."""
        return await asyncio.to_thread(self._engine.run, args_raw)


class EditTool(_Described):
    """`edit`: exact search and replace in one file, atomically; answers a unified diff. Keeps
    the head of a long diff."""

    def __init__(
        self,
        cwd: str | os.PathLike[str] | None = None,
        *,
        max_output: int = DEFAULT_MAX_OUTPUT,
        overflow_dir: str | os.PathLike[str] | None = None,
        max_overflow: int = DEFAULT_MAX_OVERFLOW,
    ) -> None:
        engine = _core.Edit(_working_directory(cwd), max_output, overflow_dir, max_overflow)
        super().__init__(engine)
        self._engine = engine

    async def call(self, args_raw: str) -> Outcome:
        """Edit the file named in `args_raw`. `Ok` with the diff, or `Err`."""
        return await asyncio.to_thread(self._engine.run, args_raw)


class FindTool(_Cancellable):
    """`find`: the files under a directory whose name matches a glob, with `grep`'s walk.
    Keeps the head."""

    def __init__(
        self,
        cwd: str | os.PathLike[str] | None = None,
        *,
        max_output: int = DEFAULT_MAX_OUTPUT,
        overflow_dir: str | os.PathLike[str] | None = None,
        max_overflow: int = DEFAULT_MAX_OVERFLOW,
    ) -> None:
        engine = _core.Find(_working_directory(cwd), max_output, overflow_dir, max_overflow)
        super().__init__(engine)


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
        if not _BASH_RUNS_HERE:
            raise OSError(
                f"the bash tool is not available on {sys.platform}: it runs on Linux and macOS"
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


def base_tools(  # noqa: PLR0913 - the settings the tools share
    cwd: str | os.PathLike[str] | None = None,
    *,
    max_output: int = DEFAULT_MAX_OUTPUT,
    overflow_dir: str | os.PathLike[str] | None = None,
    max_overflow: int = DEFAULT_MAX_OVERFLOW,
    bash_timeout_ms: int = DEFAULT_BASH_TIMEOUT_MS,
    bash_max_timeout_ms: int = DEFAULT_BASH_MAX_TIMEOUT_MS,
    bash_kill_grace_ms: int = DEFAULT_KILL_GRACE_MS,
) -> list[ReadTool | WriteTool | EditTool | GrepTool | FindTool | BashTool]:
    """`read`, `write`, `edit`, `grep`, `find` and `bash`, all working in `cwd` (default: the
    current directory). Where `bash` does not run (Windows), the other
    five."""
    tools: list[ReadTool | WriteTool | EditTool | GrepTool | FindTool | BashTool] = [
        ReadTool(cwd, max_output=max_output),
        WriteTool(cwd),
        EditTool(cwd, max_output=max_output, overflow_dir=overflow_dir, max_overflow=max_overflow),
        GrepTool(cwd, max_output=max_output, overflow_dir=overflow_dir, max_overflow=max_overflow),
        FindTool(cwd, max_output=max_output, overflow_dir=overflow_dir, max_overflow=max_overflow),
    ]
    if _BASH_RUNS_HERE:
        tools.append(
            BashTool(
                cwd,
                max_output=max_output,
                overflow_dir=overflow_dir,
                max_overflow=max_overflow,
                timeout_ms=bash_timeout_ms,
                max_timeout_ms=bash_max_timeout_ms,
                kill_grace_ms=bash_kill_grace_ms,
            )
        )
    return tools


class BaseTools:
    """The built-in extension `base_tools`: `base_tools()` as an extension's `tools`, so every
    session the shared start opens has them. Built when the extensions are loaded, in the process's
    working directory (see `load_extensions`), which the shared start has already changed to the
    session's (a program that calls `load_extensions` itself changes directory first). It has no
    options: to have fewer tools, turn it off (`load: {base_tools: false}`) and load your own."""

    name = "base_tools"

    def __init__(self) -> None:
        self.tools = tuple(base_tools())

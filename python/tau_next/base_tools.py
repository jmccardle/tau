"""The base tools: `read`, `write`, `grep`, `bash`, implemented in Rust (`crates/tau-tools`).

Each class satisfies `Tool`. The work runs on a worker thread with the GIL released, so
calls run in parallel, even on free-threaded builds, and never block the event loop.

    tools = base_tools(cwd="/work/project")
    driver = Driver(session, provider, tools=tools)

`read`, `grep` and `bash` limit their output to `max_output` bytes (default 10_000): past
it, the model gets the kept lines and a note naming the omitted line count and a file in
`<temp dir>/tau/` that holds the full output. `read` and `grep` keep the head, `bash` the
tail. `write` answers one short line, so the limit does not apply to it.

An expected failure (bad arguments, a missing file, a command that exits nonzero, an overflow
file that cannot be written) answers `Outcome.Err` with a message for the model; nothing is
raised out of `call`. A bad constructor argument raises `ValueError` or `NotADirectoryError`.
"""

import asyncio
import json
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Protocol

from tau_next import _core
from tau_next._core import Outcome

DEFAULT_MAX_OUTPUT = 10_000
DEFAULT_BASH_TIMEOUT_MS = 30_000


class _Engine(Protocol):
    @property
    def name(self) -> str: ...
    @property
    def description(self) -> str: ...
    @property
    def parameters_json(self) -> str: ...
    def run(self, args_raw: str) -> Outcome: ...


def _working_directory(cwd: str | os.PathLike[str] | None) -> Path:
    path = Path.cwd() if cwd is None else Path(cwd).absolute()
    if not path.is_dir():
        raise NotADirectoryError(f"the working directory does not exist: {path}")
    return path


class _TextTool:
    """A tool whose Rust engine runs to completion in one blocking call."""

    def __init__(self, engine: _Engine) -> None:
        self._engine = engine
        self._parameters: dict[str, object] = json.loads(engine.parameters_json)

    @property
    def name(self) -> str:
        return self._engine.name

    @property
    def description(self) -> str:
        return self._engine.description

    @property
    def parameters(self) -> Mapping[str, object]:
        return self._parameters

    async def call(self, args_raw: str) -> Outcome:
        return await asyncio.to_thread(self._engine.run, args_raw)


class ReadTool(_TextTool):
    """`read`: a text file, from line `offset` for `limit` lines. Keeps the head of long output."""

    def __init__(
        self,
        cwd: str | os.PathLike[str] | None = None,
        *,
        max_output: int = DEFAULT_MAX_OUTPUT,
        overflow_dir: str | os.PathLike[str] | None = None,
    ) -> None:
        super().__init__(_core.Read(_working_directory(cwd), max_output, overflow_dir))


class WriteTool(_TextTool):
    """`write`: a whole file, atomically, creating parent directories."""

    def __init__(self, cwd: str | os.PathLike[str] | None = None) -> None:
        super().__init__(_core.Write(_working_directory(cwd)))


class GrepTool(_TextTool):
    """`grep`: a regex over a file or a directory tree, honoring .gitignore. Keeps the head."""

    def __init__(
        self,
        cwd: str | os.PathLike[str] | None = None,
        *,
        max_output: int = DEFAULT_MAX_OUTPUT,
        overflow_dir: str | os.PathLike[str] | None = None,
    ) -> None:
        super().__init__(_core.Grep(_working_directory(cwd), max_output, overflow_dir))


class BashTool:
    """`bash`: a shell command with a timeout. Keeps the tail of long output.

    Cancelling `call` kills the command's whole process group and waits for it to end
    before the `CancelledError` propagates: no child is left running.
    """

    def __init__(
        self,
        cwd: str | os.PathLike[str] | None = None,
        *,
        max_output: int = DEFAULT_MAX_OUTPUT,
        overflow_dir: str | os.PathLike[str] | None = None,
        timeout_ms: int = DEFAULT_BASH_TIMEOUT_MS,
    ) -> None:
        self._engine = _core.Bash(_working_directory(cwd), max_output, overflow_dir, timeout_ms)
        self._parameters: dict[str, object] = json.loads(self._engine.parameters_json)

    @property
    def name(self) -> str:
        return self._engine.name

    @property
    def description(self) -> str:
        return self._engine.description

    @property
    def parameters(self) -> Mapping[str, object]:
        return self._parameters

    async def call(self, args_raw: str) -> Outcome:
        run = self._engine.start(args_raw)
        waiting = asyncio.ensure_future(asyncio.to_thread(run.wait))
        try:
            return await asyncio.shield(waiting)
        except asyncio.CancelledError:
            run.kill()
            await waiting  # returns promptly: the group is dead and the pipe closed
            raise


def base_tools(
    cwd: str | os.PathLike[str] | None = None,
    *,
    max_output: int = DEFAULT_MAX_OUTPUT,
    overflow_dir: str | os.PathLike[str] | None = None,
    bash_timeout_ms: int = DEFAULT_BASH_TIMEOUT_MS,
) -> list[ReadTool | WriteTool | GrepTool | BashTool]:
    """`read`, `write`, `grep` and `bash`, all working in `cwd` (default: the current directory)."""
    return [
        ReadTool(cwd, max_output=max_output, overflow_dir=overflow_dir),
        WriteTool(cwd),
        GrepTool(cwd, max_output=max_output, overflow_dir=overflow_dir),
        BashTool(cwd, max_output=max_output, overflow_dir=overflow_dir, timeout_ms=bash_timeout_ms),
    ]

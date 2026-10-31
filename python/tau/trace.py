"""Traces: one timeline across Rust and Python, for one run of a session.

`start(path)` makes the native module record its spans (core, store, and the
bindings) to a Chrome trace file. The driver's effects (hook calls, samples,
tool calls, and its own calls into the session) are spans too, made here with
`span`, timed on the native clock, and merged into the file by `stop()`, one
track per asyncio task. Open the file in Perfetto or chrome://tracing.

Nothing is recorded, and a `span` is one global check, until `start`.
"""

import asyncio
import contextlib
import json
from collections.abc import Generator
from pathlib import Path
from types import TracebackType
from typing import Any

from tau import _core

__all__ = ["TraceError", "capture", "span", "start", "stop"]

TraceError = _core.TraceError

# Python's tracks are numbered from here, clear of the native threads' 0, 1, 2, ...
FIRST_TRACK = 1000

_events: list[dict[str, Any]] | None = None
_tracks: dict[int, int] = {}
_path: Path | None = None


class _Span:
    __slots__ = ("_args", "_cat", "_name", "_start", "_task")

    def __init__(self, kind: str, name: str, args: dict[str, Any]) -> None:
        self._cat = kind
        self._name = name
        self._args = args
        self._task = _task_id()
        self._start = _core.trace_clock()

    def __enter__(self) -> None:
        return None

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        events = _events  # read once: a `stop()` on another thread may clear it meanwhile
        # A span that exits after `stop()` appends to the list `stop()` already wrote out, so
        # it is lost; the file holds the spans that ended before it.
        if events is None:
            return
        end = _core.trace_clock()
        track = _tracks.setdefault(self._task, FIRST_TRACK + len(_tracks))
        events.append(
            {
                "ph": "X",
                "pid": 1,
                "tid": track,
                "ts": self._start,
                "dur": end - self._start,
                "cat": self._cat,
                "name": self._name,
                "args": self._args,
            }
        )


class _Off:
    __slots__ = ()

    def __enter__(self) -> None:
        return None

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        return None


_OFF = _Off()


def _task_id() -> int:
    """The running asyncio task, or 0 outside one: effects of different tasks overlap,
    so each gets its own track; one task's spans nest."""
    try:
        task = asyncio.current_task()
    except RuntimeError:
        return 0
    return 0 if task is None else id(task)


def span(kind: str, name: str, **args: object) -> _Span | _Off:
    """A context manager timing its block as a span of `kind` (`hook`, `sample`,
    `tool`, `driver`) and `name`. A no-op unless a trace is running."""
    if _events is None:
        return _OFF
    return _Span(kind, name, args)


def start(path: str | Path) -> None:
    """Record to `path` until `stop()`. Raises `TraceError` if a trace is running."""
    global _events, _path  # noqa: PLW0603 - the one trace of this process
    if _events is not None:
        raise TraceError(f"already tracing to {_path}")
    _core.trace_to(path)
    _events = []
    _tracks.clear()
    _path = Path(path)


def stop() -> Path:
    """End the trace and write the file, native and Python spans together."""
    global _events, _path  # noqa: PLW0603 - the one trace of this process
    if _events is None or _path is None:
        raise TraceError("no trace is running")
    events, path, tracks = _events, _path, dict(_tracks)
    _events = _path = None
    _core.stop_trace()
    merged = json.loads(path.read_text())
    merged.extend(
        {
            "ph": "M",
            "pid": 1,
            "tid": track,
            "name": "thread_name",
            "args": {"name": f"python task {track - FIRST_TRACK}"},
        }
        for track in tracks.values()
    )
    merged.extend(events)
    path.write_text(json.dumps(merged))
    return path


@contextlib.contextmanager
def capture(path: str | Path) -> Generator[None]:
    """`start(path)` then, on leaving the block, `stop()`."""
    start(path)
    try:
        yield
    finally:
        stop()

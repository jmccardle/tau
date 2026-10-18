"""Capture what a tool prints, per call, without touching anyone else's output.

Experimental (experiments/tool-exposure/RESULTS.md §5). ``sys.stdout`` is one
object for the whole process, and in ``tau -p --mode json`` it IS the event
stream, so a ``print`` inside a tool writes a line that is not JSON into it. A
redirect of ``sys.stdout`` around one call would also swallow the output of every
other coroutine running at that moment, including the head's own writer.

So the redirect is a proxy installed once, and the decision is per call: the
proxy writes to the buffer in :data:`_CAPTURE` when the running context has one,
and to the stream it wraps otherwise. A context variable follows a tool into
``asyncio.to_thread`` and into tasks it creates, and does not leak into a
sibling call running in parallel.

This covers Python-level writes only. A subprocess that inherits file descriptor
1 or 0, or C code writing to fd 1, goes around it; that is a rule for whoever
spawns the process (``bash`` passes ``stdin=DEVNULL`` and pipes the rest).
"""

from __future__ import annotations

import io
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, TextIO

from tau_llm.docs import agent_facing

__all__ = ["CapturedOutput", "capture_tool_output", "suspend_capture", "MAX_CAPTURED_CHARS"]

MAX_CAPTURED_CHARS = 8192
"""Kept per stream per call; the rest is counted in ``dropped``, not stored."""


@agent_facing(topic="tools")
@dataclass
class CapturedOutput:
    """What one tool call wrote to ``sys.stdout`` and ``sys.stderr``.

    Attributes:
        stdout: The first :data:`MAX_CAPTURED_CHARS` characters written to stdout.
        stderr: Likewise for stderr.
        dropped: Characters past the cap, on both streams together.
    """

    stdout: io.StringIO = field(default_factory=io.StringIO)
    stderr: io.StringIO = field(default_factory=io.StringIO)
    dropped: int = 0

    def write(self, stream: str, text: str) -> None:
        """Append ``text`` to one stream's buffer, counting what the cap drops.

        Args:
            stream: ``"stdout"`` or ``"stderr"``.
            text: What was written.

        Raises:
            ValueError: ``stream`` is neither.
        """
        if stream == "stdout":
            buf = self.stdout
        elif stream == "stderr":
            buf = self.stderr
        else:
            raise ValueError(
                f"CapturedOutput.write: stream must be 'stdout' or 'stderr', not {stream!r}"
            )
        room = MAX_CAPTURED_CHARS - buf.tell()
        buf.write(text[: max(room, 0)])
        self.dropped += max(len(text) - max(room, 0), 0)

    def as_details(self) -> dict[str, Any] | None:
        """This call's output for a result's ``details["captured_output"]``.

        Returns:
            ``{stdout, stderr, dropped}`` with empty or zero parts left out, or
            ``None`` if nothing was written.
        """
        out = {"stdout": self.stdout.getvalue(), "stderr": self.stderr.getvalue()}
        found: dict[str, Any] = {k: v for k, v in out.items() if v}
        if self.dropped:
            found["dropped"] = self.dropped
        return found or None


_CAPTURE: ContextVar[CapturedOutput | None] = ContextVar("tau_tool_output", default=None)


class _ContextStream:
    """A ``sys.stdout``/``sys.stderr`` stand-in that routes by :data:`_CAPTURE`.

    Not an ``io.TextIOBase``: that base answers ``isatty()``, ``fileno()`` and
    ``encoding`` itself, and every one of them must come from the wrapped stream.
    """

    def __init__(self, wrapped: TextIO, stream: str) -> None:
        self.wrapped = wrapped
        self.stream = stream

    def write(self, text: str) -> int:
        """Write to the running call's buffer if there is one, else to the wrapped stream."""
        sink = _CAPTURE.get()
        if sink is None:
            return self.wrapped.write(text)
        sink.write(self.stream, text)
        return len(text)

    def flush(self) -> None:
        """Flush the wrapped stream; a buffer needs none."""
        if _CAPTURE.get() is None:
            self.wrapped.flush()

    def __getattr__(self, name: str) -> Any:
        return getattr(self.wrapped, name)


def _ensure_proxies() -> None:
    """Wrap the current ``sys.stdout``/``sys.stderr`` unless they already are proxies.

    Re-checked on every capture because a head may rebind them (the RPC
    transport points ``sys.stdout`` at stderr; Textual installs its own).
    """
    if not isinstance(sys.stdout, _ContextStream):
        sys.stdout = _ContextStream(sys.stdout, "stdout")
    if not isinstance(sys.stderr, _ContextStream):
        sys.stderr = _ContextStream(sys.stderr, "stderr")


@agent_facing(topic="tools")
@contextmanager
def capture_tool_output() -> Iterator[CapturedOutput]:
    """Capture this context's writes to ``sys.stdout``/``sys.stderr`` for the block.

    Installs the routing proxies on ``sys.stdout``/``sys.stderr`` if they are
    not already there; they stay installed afterwards.

    Returns:
        A context manager yielding the :class:`CapturedOutput` the block fills.
    """
    _ensure_proxies()
    sink = CapturedOutput()
    token = _CAPTURE.set(sink)
    try:
        yield sink
    finally:
        _CAPTURE.reset(token)


@agent_facing(topic="tools")
@contextmanager
def suspend_capture() -> Iterator[None]:
    """Let this context's writes reach the real streams for the block.

    For harness code that runs inside a tool's context but writes for a head:
    an event emitted from a nested call reaches a synchronous subscriber, such as
    the ``--mode json`` writer, in the calling tool's context.

    Returns:
        A context manager yielding ``None``.
    """
    token = _CAPTURE.set(None)
    try:
        yield
    finally:
        _CAPTURE.reset(token)

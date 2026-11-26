"""`tau -p`'s signals (D22 section 6): the first SIGINT, SIGTERM or SIGHUP cancels the run, which
aborts the cursor and closes the session; a second one of any of them closes the session at once
and exits within `LEAVE_SECONDS`, finished or not. Every way out runs `Driver.stop`, and with it
each extension's `stop` (`tau._signals`), unless that close outlasts `LEAVE_SECONDS`.
"""

import asyncio
import contextlib
import os
import signal
import sys
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TextIO

from tau._signals import off_ending, on_ending, status

LEAVE_SECONDS = 2.0
"""After a second signal, the time the session has to close (the driver stopped, each extension's
`stop` run, hook commands and MCP servers ended) before the process exits without waiting more;
also the bound on the abort a SIGTERM or SIGHUP waits for."""
STOP_NOTICE = 2.0  # seconds a stop takes before it is said why


@dataclass
class Stopping:
    """How the run is being stopped: the first signal it got, whether a second one asked it to
    leave, and whether the session is closing already (which no signal may cut short)."""

    signal: int | None = None
    leaving: bool = False
    closing: bool = False
    deadline: list[threading.Timer] = field(default_factory=lambda: [])


def on_sigint(loop: asyncio.AbstractEventLoop, then: Callable[[], None]) -> None:
    """Run `then` in `loop` on the next SIGINT (Ctrl+C), in place of any handler before it.
    Windows' event loop has no `add_signal_handler`: there the handler is the interpreter's,
    which runs in the main thread and hands `then` to the loop."""
    if sys.platform == "win32":
        signal.signal(signal.SIGINT, lambda *_: loop.call_soon_threadsafe(then))
    else:
        loop.add_signal_handler(signal.SIGINT, then)


def off_sigint(loop: asyncio.AbstractEventLoop) -> None:
    """SIGINT back to the interpreter's default."""
    if sys.platform == "win32":
        signal.signal(signal.SIGINT, signal.default_int_handler)
    else:
        loop.remove_signal_handler(signal.SIGINT)


class Signals:
    """`tau -p`'s handlers while it runs: the first SIGINT, SIGTERM or SIGHUP cancels the run
    (`_main` aborts and closes); a second of any of them leaves (`again`)."""

    def __init__(
        self,
        out: TextIO,
        err: TextIO,
        stopping: "Stopping",
        loop: asyncio.AbstractEventLoop,
        task: "asyncio.Task[int]",
    ) -> None:
        self.out, self.err, self.stopping = out, err, stopping
        self.loop, self.task = loop, task
        self.hint: list[asyncio.TimerHandle] = []
        on_sigint(loop, lambda: self.first(signal.SIGINT))
        on_ending(loop, self.first)

    def off(self) -> None:
        off_sigint(self.loop)
        off_ending(self.loop)
        for timer in self.hint:
            timer.cancel()

    def first(self, number: int) -> None:
        self.stopping.signal = number
        on_sigint(self.loop, lambda: self.again(signal.SIGINT))
        on_ending(self.loop, self.again)
        if number == signal.SIGINT:  # a person is there to read it
            self.hint.append(self.loop.call_later(STOP_NOTICE, self.waiting))
        self.task.cancel()

    def again(self, _number: int) -> None:
        """A second signal: close the session at once (no more waiting for the abort), and exit
        after `LEAVE_SECONDS` whether or not the close has finished."""
        stopping = self.stopping
        if stopping.leaving:
            return
        stopping.leaving = True
        self.say("tau: interrupted\n")
        code = status(stopping.signal or signal.SIGINT)
        timer = threading.Timer(LEAVE_SECONDS, _leave, (self.out, self.err, code))
        timer.daemon = True  # a thread: it fires though a hook holds the loop
        timer.start()
        stopping.deadline.append(timer)
        if not stopping.closing:
            self.task.cancel()

    def waiting(self) -> None:
        self.say(
            "tau: still stopping (a hook is running); Ctrl+C again leaves at once, "
            "and then the message stays in the session\n"
        )

    def say(self, text: str) -> None:
        """A line on stderr, unless the terminal it was is gone."""
        with contextlib.suppress(OSError, ValueError):
            self.err.write(text)
            self.err.flush()


def _leave(out: TextIO, err: TextIO, code: int) -> None:
    """The last resort after a second signal: exit now with `code`, though the session has not
    finished closing (a hook that never returns, an extension slow to stop)."""
    try:
        with contextlib.suppress(OSError, ValueError):
            err.flush()
            out.flush()
    finally:
        os._exit(code)
